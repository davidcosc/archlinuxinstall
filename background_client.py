#!/usr/bin/python


import os
import select
import socket
import struct
import time
from collections import deque


WL_DISPLAY_OBJECT_ID = 1
WL_DISPLAY_GET_REGISTRY_OPCODE = 1

WL_REGISTRY_OBJECT_ID = 2
WL_REGISTRY_EVENT_GLOBAL_OPCODE = 0


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

		os.write(1, b"running task\n")
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


def write(fd, data):
	data_length = len(data)
	num_bytes_written = 0

	while num_bytes_written < data_length:
		num_bytes_written += os.write(fd, data[num_bytes_written:])

	os.write(1, b"C -> S: " + data.hex().encode("utf-8") + b"\n")


def wl_display_get_registry(wl_display_fd):
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
	write(wl_display_fd, msg.serialize())


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


def decode_wl_registry_event_global(event_args):
	name, interface_str_len = struct.unpack("=II", event_args[:8])
	interface = event_args[8:8 + interface_str_len][:-1].decode("utf-8")
	version = struct.unpack("=I", event_args[-4:])[0]
	os.write(1, f"args: name->{name}, interface->{interface}, version->{version}\n".encode("utf-8"))


def decode_events(events):
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
		

		match (o_id, opcode):
			case (
				WL_REGISTRY_OBJECT_ID,
				WL_REGISTRY_EVENT_GLOBAL_OPCODE
			):
				os.write(1, f"o_id: {o_id}, size: {size}, opcode: {opcode}, ".encode("utf-8"))
				decode_wl_registry_event_global(args)
			case _:
				os.write(1, f"o_id: {o_id}, size: {size}, opcode: {opcode}, args: {args}\n".encode("utf-8"))


def main():
	tasks = TaskQueue()
	wl_display_socket = connect_to_wl_display()
	wl_display_sock_fd = wl_display_socket.fileno()
	tasks.add_task(Task(wl_display_get_registry, wl_display_sock_fd))

	while True:
		rlist, wlist, _ = select.select(
			[wl_display_sock_fd],
			[wl_display_sock_fd],
			[],
			0
		)
		
		if rlist:
			events = os.read(wl_display_sock_fd, 4096)
			decode_events(events)

		if wlist:
			tasks.work_task()

		time.sleep(1)



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