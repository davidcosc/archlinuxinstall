#!/usr/bin/python
"""
From https://wayland-book.com/protocol-design/high-level.html:
When processing this XML file, we assign each request and event an opcode in 
the order that they appear, numbered from zero and incrementing independently. 
Combined with the list of arguments, you can decode the request or event when 
it comes in over the wire.

From https://wayland.freedesktop.org/docs/book/Protocol.html#wire-format:
new_id
    The 32-bit object ID. Generally, the interface used for the new object is
    inferred from the xml, but in the case where it’s not specified, a new_id
    is preceded by a string specifying the interface name, and a uint specifying
    the version.
"""

import mmap
import os
import select
import socket
import struct
import time
from collections import deque
from pathlib import Path
from PIL import Image


PROTOCOL={
	"wl_display": {
		"requests": {
			"sync": (0, ("new_id",), "wl_callback"),
			"get_registry": (1, ("new_id",), "wl_registry")
		},
		"events": {
			0: ("error", ("object", "uint", "string")),
			1: ("delete_id", ("uint",))
		}
	},
	"wl_registry": {
		"requests": {
			"bind": (0, ("uint", "string", "uint", "new_id"), "n/a")
		},
		"events": {
			0: ("global", ("uint", "string", "uint")),
			1: ("global_remove", ("uint",))
		}
	}
}


# ------------------------------------------------------------------------------
# STATE CREATION
# ------------------------------------------------------------------------------
def init_state():
	return {
		"sock": None,
		"next_object_id": 0,
		"released_ids": deque(),
		"objects": {},
		"object_ids": {},
		"object_names": {},
		"listeners": {},
		"out_queue": deque(),
		"in_queue": deque()
	}


# ------------------------------------------------------------------------------
# OBJECT CREATION/DESTRUCTION
# ------------------------------------------------------------------------------
def create_object(state, interface):
	if state["released_ids"]:
		object_id = state["released_ids"].popleft()
	else:
		state["next_object_id"] += 1
		object_id = state["next_object_id"]
		if object_id > 0xfeffffff:
			raise RuntimeError("Ran out of object ids")
	state["objects"][object_id] = interface
	if not state["object_ids"].get(interface):
		state["object_ids"][interface] = [object_id]
	else:
		state["object_ids"][interface].append(object_id)
	return object_id


def destroy_object(state, object_id):
	interface = state["objects"][object_id]
	state["objects"].pop(object_id)
	if len(state["object_ids"][interface]) > 1:
		state["object_ids"][interface].remove(object_id)
	else:
		state["object_ids"].pop(interface)
	state["released_ids"].append(object_id)
	return object_id


# ------------------------------------------------------------------------------
# EVENT SUBSCRIPTION
# ------------------------------------------------------------------------------
def listen(state, object_id, event, handler):
	interface = state["objects"][object_id]
	for opcode, (name, arg_types) in PROTOCOL[interface]["events"].items():
		if name == event:
			state["listeners"][(object_id, opcode)] = {
				"arg_types": arg_types,
				"handler": handler
			}
			return
	raise RuntimeError(f"Could not create listener for event {event}")


# ------------------------------------------------------------------------------
# WAYLAND WIRE PROTOCOL HANDLING
# ------------------------------------------------------------------------------
def pad4(n):
	return (4 - (n % 4)) % 4


def encode_arg(arg_type, value):
	if value is None:
		return struct.pack("=I", 0)
	if arg_type == "string":
		data = value.encode("utf-8") + b"\x00"
		len_data = len(data)
		data = data + b"\x00" * pad4(len_data)
		return struct.pack("=I", len_data) + data
	return struct.pack("=I", value)


def enqueue_encoded_message(state, object_id, request, *args):
	interface = state["objects"][object_id]
	opcode, arg_types, _ = PROTOCOL[interface]["requests"][request]
	type_arg_tuples = []
	aux = None
	for index, arg_type in enumerate(arg_types):
		if arg_type == "fd":
			aux = (
				socket.SOL_SOCKET,
				socket.SCM_RIGHTS,
				struct.pack("=i", args[index])
			)
			continue
		type_arg_tuples.append((arg_type, args[index]))
	encoded_args = b"".join(encode_arg(k, v) for k, v in type_arg_tuples)
	size = 8 + len(encoded_args)
	header = struct.pack("=II", object_id, (size << 16) | opcode)
	encoded_msg = header + encoded_args
	print(f"C -> S: {encoded_msg.hex()}", flush=True)
	msg = {"payload": encoded_msg, "aux": aux}
	state["out_queue"].append(msg)
	return msg


def decode_args(args, arg_types):
	offset = 0
	decoded_args = []
	for arg_type in arg_types:
		first_int = struct.unpack("=I", args[offset:offset + 4])[0]
		offset += 4
		if arg_type == "string":
			slen = first_int
			sbytes = args[offset:offset + slen]
			decoded_args.append(sbytes[:-1].decode("utf-8"))
			offset += slen + pad4(slen)
		else:
			arg = first_int
			decoded_args.append(arg)
	return decoded_args


def enqueue_decoded_messages(state, data):
	offset = 0
	while offset < len(data):
		object_id, size_opcode = struct.unpack(
			"=II",
			data[offset:offset + 8]
		)
		size = size_opcode >> 16
		opcode = size_opcode & 0xffff
		args = data[offset + 8:offset + size]
		print(f"S -> C: {data[offset:offset + size].hex()}", flush=True)
		offset += size
		listener = state["listeners"].get((object_id, opcode))
		if not listener:
			print(
				f"No listener for obj {object_id} op {opcode}",
				flush=True
			)
			continue
		arg_types = listener["arg_types"]
		handler = listener["handler"]
		decoded_args = decode_args(args, arg_types)
		state["in_queue"].append((handler, decoded_args))
		

# ------------------------------------------------------------------------------
# I/O HANDLING
# ------------------------------------------------------------------------------
def connect(state):
	# https://wayland-book.com/protocol-design/wire-protocol.html#transports
	# we do not check WAYLAND_SOCKET since this client is not intended to be
	# used as a subclient
	runtime_dir = os.environ["XDG_RUNTIME_DIR"]

	if not runtime_dir:
		exit(1)
	
	display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
	path = os.path.join(runtime_dir, display)
	sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	sock.connect(path)
	state["sock"] = sock


def flush_out_queue(state):
	sock = state["sock"]
	while state["out_queue"]:
		msg = state["out_queue"].popleft()
		data = msg["payload"]
		auxdata = [msg["aux"]] if msg["aux"] else []
		num_sent = 0
		while num_sent < len(data):
			num_sent += sock.sendmsg(
				[data[num_sent:]],
				auxdata if num_sent == 0 else []
			)


def read_to_in_queue(state):
	data = os.read(state["sock"].fileno(), 4096)
	if data == b"":
		raise ConnectionError("Compositor closed connection")
	enqueue_decoded_messages(state, data)


# ------------------------------------------------------------------------------
# ROUTING AND EVENT HANDLING
# ------------------------------------------------------------------------------
def dispatch(state):
	while state["in_queue"]:
		callback, args = state["in_queue"].popleft()
		callback(state, *args)


def on_error(state, object_id, code, message):
	print(f'Error: {state["objects"][object_id]} code {code} msg {message}')


def on_global(state, name, interface, version):
	print(f"Global: {name} {interface} {version}")


# ------------------------------------------------------------------------------
# EVENT LOOP
# ------------------------------------------------------------------------------
def main():
	state = init_state()
	connect(state)
	create_object(state, "wl_display")
	listen(
		state,
		state["object_ids"]["wl_display"][0],
		"error",
		on_error
	)
	create_object(state, "wl_registry")
	listen(
		state,
		state["object_ids"]["wl_registry"][0],
		"global",
		on_global
	)
	enqueue_encoded_message(
		state,
		state["object_ids"]["wl_display"][0],
		"get_registry",
		state["object_ids"]["wl_registry"][0]
	)
	while True:
		if state["out_queue"]:
			flush_out_queue(state)
		rlist, _, _ = select.select([state["sock"].fileno()], [], [])
		if rlist:
			read_to_in_queue(state)
			dispatch(state)


if __name__ == "__main__":
	main()