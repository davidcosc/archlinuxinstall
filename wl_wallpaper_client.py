#!/usr/bin/python
"""
From https://wayland-book.com/protocol-design/high-level.html:
When processing this XML file, we assign each request and event an opcode in 
the order that they appear, numbered from zero and incrementing independently. 
Combined with the list of arguments, you can decode the request or event when 
it comes in over the wire, and based on the documentation shipped in the XML 
file you can decide how to program your software to behave accordingly. This 
usually comes in the form of code generation — we'll talk about how libwayland 
does this in chapter 3.
"""

import mmap
import os
import select
import socket
import struct
import time
from collections import deque
from enum import IntFlag


class WlConnection:
	def __init__(self):
		self.socket = None
		self.sock_fd = -1
		self.out_messages = deque()
		self.in_messages = deque()
		self.event_callbacks = {}

	def connect(self):
		# https://wayland-book.com/protocol-design/wire-protocol.html#transports
		# we do not check WAYLAND_SOCKET since this client is not intended to be
		# used as a subclient
		runtime_dir = os.environ["XDG_RUNTIME_DIR"]

		if not runtime_dir:
			exit(1)
		
		display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
		path = os.path.join(runtime_dir, display)
		self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.socket.connect(path)
		self.sock_fd = self.socket.fileno()

	def calc_num_pad_bytes(self, data):
		# https://wayland.freedesktop.org/docs/book/Protocol.html#string
		remainder = len(data) % 4
		return (4 - remainder) % 4

	def encode_arg(self, arg):
		if isinstance(arg, int):
			return struct.pack("=I", arg)

		# https://wayland.freedesktop.org/docs/book/Protocol.html#string
		elif isinstance(arg, str):
			data = arg.encode("utf-8") + b"\x00"
			return (
				struct.pack("=I", len(data))
				+ data
				+ b"\x00" * self.calc_num_pad_bytes(data)
			)

	def decode_args(self, args, arg_types):
		offset = 0
		decoded_args = []

		for arg_type in arg_types:
			if arg_type is int:
				decoded_args.append(
					struct.unpack(
						"=I",
						args[offset:offset + 4]
					)[0]
				)
				offset += 4

			elif arg_type is str:
				arg_len = struct.unpack(
					"=I",
					args[offset:offset + 4]
				)[0]
				offset += 4
				arg = args[offset:offset + arg_len]
				decoded_args.append(arg[:-1].decode("utf-8"))
				offset += arg_len
				offset += self.calc_num_pad_bytes(arg)

		return decoded_args

	def register_event(self, object_id, opcode, arg_types, callback):
		self.event_callbacks[(object_id, opcode)] = {
			"arg_types": arg_types,
			"callback": callback
		}

	def register_request(self, object_id, opcode, *args):
		self.out_messages.append((object_id, opcode, args))

	def sendall(self, data):
		data_length = len(data)
		num_bytes_written = 0

		while num_bytes_written < data_length:
			num_bytes_written += os.write(
				self.sock_fd,
				data[num_bytes_written:]
			)

		print(f"C -> S: {data.hex()}")

	def send_messages(self):
		while self.out_messages:
			msg = self.out_messages.popleft()
			encoded_args = b"".join(
				self.encode_arg(a) for a in msg[2]
			)
			message_size = 8 + len(encoded_args)
			encoded_header = struct.pack(
				"=II",
				msg[0],
				(message_size << 16) | msg[1]
			)
			self.sendall(encoded_header + encoded_args)

	def receive_messages(self):
		offset = 0
		data = os.read(self.sock_fd, 4096)
		print(data, flush=True)

		if data == b"":
			exit(1)
		
		while offset < len(data):
			object_id, size_opcode = struct.unpack(
				"=II",
				data[offset:offset + 8]
			)
			size = size_opcode >> 16
			opcode = size_opcode & 0xFFFF
			args = data[offset + 8: offset + size]
			offset += size
			event_cb = self.event_callbacks.get((object_id, opcode))

			if not event_cb:
				print(f"Ignoring object_id: {object_id}, size: {size}, opcode: {opcode}, args: {args}", flush=True)
				continue
			
			arg_types = event_cb["arg_types"]
			callback = event_cb["callback"]
			decoded_args = self.decode_args(args, arg_types)
			self.in_messages.append(
				(object_id, opcode, decoded_args, callback)
			)

	def dispatch(self):
		while self.in_messages:
			msg = self.in_messages.popleft()
			object_id, opcode, args, callback = msg
			callback(*args)


class WlDisplay:
	"""
	<interface name="wl_display" version="1">
		<request name="sync">
			<arg name="callback" type="new_id" interface="wl_callback"/>
		</request>

		<request name="get_registry">
			<arg name="registry" type="new_id" interface="wl_registry"/>
		</request>

		<event name="error">
			<arg name="object_id" type="object"/>
			<arg name="code" type="uint"/>
			<arg name="message" type="string"/>
		</event>

		<event name="delete_id">
			<arg name="id" type="uint" />
		</event>
	</interface>
	"""

	def __init__(self, con):
		self.con = con
		self.object_id = 1
		

	def register_request_get_registry(self, new_id):
		opcode = 1
		self.con.register_request(self.object_id, opcode, new_id)


class WlRegistry:
	"""
	<interface name="wl_registry" version="1">
		<request name="bind">
			<arg name="name" type="uint" summary="unique name for the object"/>
			<arg name="id" type="new_id"/>
		</request>

		<event name="global">
			<arg name="name" type="uint"/>
			<arg name="interface" type="string"/>
			<arg name="version" type="uint"/>
		</event>

		<event name="global_remove">
			<arg name="name" type="uint"/>
		</event>
	</interface>
	"""

	def __init__(self, con):
		self.con = con
		self.object_id = 2
		self.recv_global_events = {}

	def handle_event_global(self, name, interface, version):
		print(f"Adding global event: {name}, {interface}, {version}", flush=True)
		self.recv_global_events[interface] = {
			"name": name,
			"version": version
		}
		
	def register_event_global(self):
		opcode = 0
		arg_types = (int, str, int)
		self.con.register_event(
			self.object_id,
			opcode,
			arg_types,
			self.handle_event_global
		)

	def register_request_bind(self, name, interface, version, new_id):
		opcode = 0
		self.con.register_request(
			self.object_id,
			opcode,
			name,
			interface,
			version,
			new_id
		)


class WlCompositor:
	"""
	<interface name="wl_compositor" version="4">
		<request name="create_surface">
			<arg name="id" type="new_id" interface="wl_surface"/>
		</request>

		<request name="create_region">
			<arg name="id" type="new_id" interface="wl_region"/>
		</request>
	</interface>
	"""
	def __init__(self, con):
		self.object_id = 3
		self.con = con

	def register_request_ceeate_surface(self, new_id):
		opcode = 0
		self.con.register_request(self.object_id, opcode, new_id)


class WlSurface:
	def __init__(self, con):
		self.object_id = 4
		self.con = con


class Client:
	def __init__(self):
		self.con = WlConnection()
		self.display = None
		self.registry = None
		self.compositor = None
		self.surface = None

	def start(self):
		self.con.connect()
		self.display = WlDisplay(self.con)
		self.registry = WlRegistry(self.con)
		self.registry.register_event_global()
		self.display.register_request_get_registry(
			self.registry.object_id
		)

	def step(self):
		if not self.display and self.registry:
			raise Exception("Client not started")
		
		comp = self.registry.recv_global_events.get("wl_compositor")

		if comp and not self.compositor:
			self.compositor = WlCompositor(self.con)
			self.registry.register_request_bind(
				comp["name"],
				"wl_compositor",
				comp["version"],
				self.compositor.object_id
			)
			print(f"WlCompositor created", flush=True)

		if self.compositor and not self.surface:
			self.surface = WlSurface(self.con)
			self.compositor.register_request_ceeate_surface(
				self.surface.object_id
			)
			print(f"WlSurface created", flush=True)
		

def main():
	client = Client()
	client.start()

	while True:
		client.step()

		rlist, wlist, _ = select.select(
			[client.con.sock_fd],
			[client.con.sock_fd],
			[],
			0
		)
		
		if rlist:
			client.con.receive_messages()

		if wlist:
			client.con.send_messages()

		client.con.dispatch()
		time.sleep(3)
	


if __name__ == "__main__":
	main()
