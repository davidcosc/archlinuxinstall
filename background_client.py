#!/usr/bin/python


import mmap
import os
import select
import socket
import struct
import time
from collections import deque
from enum import IntFlag


WL_DISPLAY_OBJECT_ID = 1
WL_DISPLAY_GET_REGISTRY_OPCODE = 1

WL_REGISTRY_OBJECT_ID = 2
WL_REGISTRY_EVENT_GLOBAL_OPCODE = 0
WL_REGISTRY_BIND_OPCODE = 0

WL_SHM_OBJECT_ID = 3
WL_SHM_FORMAT_XRGB8888 = 0x00000001


class Flags(IntFlag):
    START  = 0
    CONN_DISP  = 1 << 0
    REG_BIND = 1 << 1
    SHM_BIND  = 1 << 2


class Task:
	def __init__(self, func, *args):
		self.func = func
		self.args = args

	def run(self):
		return self.func(*self.args)


class TaskQueue:
	def __init__(self):
		self.q = deque()

	def add_task(self, task):
		self.q.append(task)

	def work_task(self):
		if not self.q:
			return None

		task = self.q.popleft()
		return task.run()


# https://wayland-book.com/protocol-design/wire-protocol.html#messages
class WaylandWireProtocolMessage:
	def __init__(self, o_id, opcode, msg_args=None):
		self.o_id = o_id
		self.opcode = opcode
		self.msg_args = msg_args

	def serialize(self):
		if self.o_id > 0xFEFFFFFF:
			exit(1)

		size = 8 + len(self.msg_args)

		# the upper 16 bits are the size of the message
		# and the lower 16 bits are the event or request opcode
		return struct.pack(
			"=II",
			self.o_id,
			(size << 16) | self.opcode
		) + self.msg_args


def connect_to_wl_display():
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
	return sock


def write(fd, data):
	data_length = len(data)
	num_bytes_written = 0

	while num_bytes_written < data_length:
		num_bytes_written += os.write(fd, data[num_bytes_written:])

	os.write(1, b"C -> S: " + data.hex().encode("utf-8") + b"\n")


def wl_display_get_registry(client_state):
	"""
	<interface name="wl_display" version="1">
		<request name="sync">
		<arg name="callback" type="new_id" interface="wl_callback" />
		</request>

		<request name="get_registry">
		<arg name="registry" type="new_id" interface="wl_registry" />
		</request>

		<!-- ... -->
	</interface>
	"""
	msg = WaylandWireProtocolMessage(
		WL_DISPLAY_OBJECT_ID,
		WL_DISPLAY_GET_REGISTRY_OPCODE,
		msg_args=struct.pack("=I", 2)
	)
	write(client_state["wl_display_sock_fd"], msg.serialize())
	client_state["state"] |= Flags.REG_BIND


def pad_bytes(to_pad):
	remainder = len(to_pad) % 4
	num_pad= (4 - remainder) % 4
	padding = b"\x00" * num_pad
	return to_pad + padding


def wl_registry_bind(client_state, name, interface, version, new_id):
	# https://wayland.freedesktop.org/docs/book/Protocol.html#new_id
	#  The 32-bit object ID. Generally, the interface used for the new
	# object is inferred from the xml, but in the case where it’s not
	# specified, a new_id is preceded by a string specifying the interface
	# name, and a uint specifying the version.
	interface_str_len = len(interface) + 1
	padded_interface = pad_bytes(interface + b"\x00")
	msg_args = (
		struct.pack("=II", name, interface_str_len)
		+ padded_interface
		+ struct.pack("=II", version, new_id)
	)
	msg = WaylandWireProtocolMessage(
		WL_REGISTRY_OBJECT_ID,
		WL_REGISTRY_BIND_OPCODE,
		msg_args=msg_args
	)
	write(client_state["wl_display_sock_fd"], msg.serialize())


def create_shared_frame_buffer(size):
	fd = os.memfd_create("background_client_frame_buffer")
	os.ftruncate(fd, size)
	shared_frame_buffer = mmap.mmap(
		fd,
		size,
		flags=mmap.MAP_SHARED,
		prot=mmap.PROT_READ | mmap.PROT_WRITE,
	)
	return fd, shared_frame_buffer


def handle_wl_registry_event_global(client_state, event_args):
	name, interface_str_len = struct.unpack("=II", event_args[:8])
	interface = event_args[8:8 + interface_str_len][:-1]
	version = struct.unpack("=I", event_args[-4:])[0]

	if interface == b"wl_shm":
		client_state["tasks"].add_task(
			Task(
				wl_registry_bind,
				client_state,
				name,
				interface,
				version,
				WL_SHM_OBJECT_ID
			)
		)
		client_state["state"] |= Flags.SHM_BIND
	else:
		pass


def handle_wl_events(events, client_state):
	"""
	<interface name="wl_registry" version="1">
		<request name="bind">
		<arg name="name" type="uint" />
		<arg name="id" type="new_id" />
		</request>

		<event name="global">
		<arg name="name" type="uint" />
		<arg name="interface" type="string" />
		<arg name="version" type="uint" />
		</event>

		<event name="global_remove">
		<arg name="name" type="uint" />
		</event>
	</interface>	
	"""
	if events == b"":
		exit(1)

	offset = 0
	
	while offset < len(events):
		o_id, size_opcode = struct.unpack(
			"=II",
			events[offset:offset + 8]
		)
		size = size_opcode >> 16
		opcode = size_opcode & 0xFFFF
		args = events[offset + 8: offset + size]
		offset += size
		os.write(1, f"o_id: {o_id}, size: {size}, opcode: {opcode}, args: {args}".encode("utf-8"))

		if (o_id, opcode) == (
			WL_REGISTRY_OBJECT_ID,
			WL_REGISTRY_EVENT_GLOBAL_OPCODE
		):
			handle_wl_registry_event_global(
				client_state,
				args
			)
			os.write(1, b" handled\n")
		else:
			os.write(1, b" ignored\n")


def event_loop(client_state):
	while True:
		rlist, wlist, _ = select.select(
			[client_state["wl_display_sock_fd"]],
			[client_state["wl_display_sock_fd"]],
			[],
			0
		)
		
		if rlist:
			events = os.read(
				client_state["wl_display_sock_fd"],
				4096
			)
			handle_wl_events(events, client_state)

		if wlist:
			client_state["tasks"].work_task()

		time.sleep(1)


def main():
	client_state = {}
	client_state["state"] = Flags.START
	client_state["tasks"] = TaskQueue()
	client_state["wl_display_socket"] = connect_to_wl_display()
	client_state["wl_display_sock_fd"] = client_state["wl_display_socket"].fileno()
	client_state["tasks"].add_task(Task(
		wl_display_get_registry,
		client_state)
	)
	event_loop(client_state)


if __name__ == "__main__":
	main()


"""
Notes:

From https://wayland-book.com/protocol-design/high-level.html:
When processing this XML file, we assign each request and event an opcode in 
the order that they appear, numbered from zero and incrementing independently. 
Combined with the list of arguments, you can decode the request or event when 
it comes in over the wire, and based on the documentation shipped in the XML 
file you can decide how to program your software to behave accordingly. This 
usually comes in the form of code generation — we'll talk about how libwayland 
does this in chapter 3.
"""