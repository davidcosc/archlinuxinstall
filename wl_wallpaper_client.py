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
from enum import Enum, IntFlag


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

		elif arg is None:
			return struct.pack("=I", 0)

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

	def register_request(self, object_id, opcode, *args, aux=()):
		msg = {
			"params": (object_id, opcode, args),
			"aux": aux
		}
		self.out_messages.append(msg)

	def sendall(self, data, aux):
		num_bytes_written = 0
		auxdata = [aux] if aux else []

		while num_bytes_written < len(data):
			num_bytes_written += self.socket.sendmsg(
				[data[num_bytes_written:]],
				auxdata
			)

		print(f"C -> S: {data.hex()}", flush=True)

	def send_messages(self):
		while self.out_messages:
			msg = self.out_messages.popleft()
			encoded_args = b"".join(
				self.encode_arg(a) for a in msg["params"][2]
			)
			message_size = 8 + len(encoded_args)
			encoded_header = struct.pack(
				"=II",
				msg["params"][0],
				(message_size << 16) | msg["params"][1]
			)
			self.sendall(encoded_header + encoded_args, msg["aux"])

	def receive_messages(self):
		offset = 0
		data = os.read(self.sock_fd, 4096)

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
			print(f"S -> C: {data[offset:offset + size].hex()}", flush=True)
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
		self.rcvd_g_events = {}

	def handle_event_global(self, name, interface, version):
		# print(f"Adding global event: {name}, {interface}, {version}", flush=True)
		self.rcvd_g_events[interface] = {
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
	<interface name="wl_compositor" version="6">
		<request name="create_surface">
			<arg name="id" type="new_id" interface="wl_surface" summary="the new surface"/>
		</request>

		<request name="create_region">
			<arg name="id" type="new_id" interface="wl_region" summary="the new region"/>
		</request>
	</interface>
	"""

	def __init__(self, con):
		self.object_id = 3
		self.con = con

	def register_request_create_surface(self, new_id):
		opcode = 0
		self.con.register_request(self.object_id, opcode, new_id)


class WlSurface:
	"""
	<interface name="wl_surface" version="4">
		<request name="destroy" type="destructor">
		</request>

		<request name="attach">
			<arg name="buffer" type="object" interface="wl_buffer" allow-null="true"/>
			<arg name="x" type="int"/>
			<arg name="y" type="int"/>
		</request>

		<request name="damage">
			<arg name="x" type="int"/>
			<arg name="y" type="int"/>
			<arg name="width" type="int"/>
			<arg name="height" type="int"/>
		</request>

		<request name="frame">
			<arg name="callback" type="new_id" interface="wl_callback"/>
		</request>

		<request name="set_opaque_region">
			<arg name="region" type="object" interface="wl_region" allow-null="true"/>
		</request>

		<request name="set_input_region">
			<arg name="region" type="object" interface="wl_region" allow-null="true"/>
		</request>

		<request name="commit">
		</request>

		<event name="enter">
			<arg name="output" type="object" interface="wl_output"/>
		</event>

		<event name="leave">
			<arg name="output" type="object" interface="wl_output"/>
		</event>

		<request name="set_buffer_transform" since="2">
			<arg name="transform" type="int"/>
		</request>

		<request name="set_buffer_scale" since="3">
			<arg name="scale" type="int"/>
		</request>

		<request name="damage_buffer" since="4">
			<arg name="x" type="int"/>
			<arg name="y" type="int"/>
			<arg name="width" type="int"/>
			<arg name="height" type="int"/>
		</request>
	</interface>
	"""

	def __init__(self, con):
		self.object_id = 4
		self.con = con

	def register_request_attach(self, buffer, x, y):
		opcode = 1
		self.con.register_request(
			self.object_id,
			opcode,
			buffer,
			x,
			y
		)

	def register_request_commit(self):
		opcode = 6
		self.con.register_request(self.object_id, opcode)


class ZwlrLayerShellV1:
	"""
	<interface name="zwlr_layer_shell_v1" version="4">
		<request name="get_layer_surface">
			<arg name="id" type="new_id" interface="zwlr_layer_surface_v1"/>
			<arg name="surface" type="object" interface="wl_surface"/>
			<arg name="output" type="object" interface="wl_output" allow-null="true"/>
			<arg name="layer" type="uint" enum="layer" summary="layer to add this surface to"/>
			<arg name="namespace" type="string" summary="namespace for the layer surface"/>
		</request>

		<enum name="layer">
			<entry name="background" value="0"/>
			<entry name="bottom" value="1"/>
			<entry name="top" value="2"/>
			<entry name="overlay" value="3"/>
		</enum>

		<request name="destroy" type="destructor" since="3">
		</request>
	</interface>
	"""

	def __init__(self, con):
		self.object_id = 5
		self.con = con

	def register_request_get_layer_surface(
		self,
		new_id,
		surface,
		output,
		layer,
		namespace
	):
		opcode = 0
		self.con.register_request(
			self.object_id,
			opcode,
			new_id,
			surface,
			output,
			layer,
			namespace
		)


class ZwlrLayerSurfaceV1:
	"""
	You create wl_surface.
	You call zwlr_layer_shell_v1.get_layer_surface(...).
	This assigns the layer-surface role to your wl_surface.
	You configure the layer surface with requests such as set_size, set_anchor, etc.
	You wl_surface.commit() with no buffer attached.
	Compositor sends zwlr_layer_surface_v1.configure.
	You acknowledge the configure with ack_configure.
	You create/obtain an actual wl_buffer through something like wl_shm.
	You attach that buffer to your existing wl_surface.
	You wl_surface.commit() again, this time with the buffer attached.
	<interface name="zwlr_layer_surface_v1" version="4">
		<request name="set_size">
			<arg name="width" type="uint"/>
			<arg name="height" type="uint"/>
		</request>

		<request name="set_anchor">
			<arg name="anchor" type="uint" enum="anchor"/>
		</request>

		<request name="set_exclusive_zone">
			<arg name="zone" type="int"/>
		</request>

		<request name="set_margin">
			<arg name="top" type="int"/>
			<arg name="right" type="int"/>
			<arg name="bottom" type="int"/>
			<arg name="left" type="int"/>
		</request>

		<enum name="keyboard_interactivity">
			<entry name="none" value="0">
			</entry>
			<entry name="exclusive" value="1">
			</entry>
			<entry name="on_demand" value="2" since="4">
			</entry>
		</enum>

		<request name="set_keyboard_interactivity">
			<arg name="keyboard_interactivity" type="uint" enum="keyboard_interactivity"/>
		</request>

		<request name="get_popup">
			<arg name="popup" type="object" interface="xdg_popup"/>
		</request>

		<request name="ack_configure">
			<arg name="serial" type="uint" summary="the serial from the configure event"/>
		</request>

		<request name="destroy" type="destructor"/>

		<event name="configure">
			<arg name="serial" type="uint"/>
			<arg name="width" type="uint"/>
			<arg name="height" type="uint"/>
		</event>

		<event name="closed"/>

		<enum name="anchor" bitfield="true">
			<entry name="top" value="1" summary="the top edge of the anchor rectangle"/>
			<entry name="bottom" value="2" summary="the bottom edge of the anchor rectangle"/>
			<entry name="left" value="4" summary="the left edge of the anchor rectangle"/>
			<entry name="right" value="8" summary="the right edge of the anchor rectangle"/>
		</enum>

		<request name="set_layer" since="2">
			<arg name="layer" type="uint" enum="zwlr_layer_shell_v1.layer" summary="layer to move this surface to"/>
		</request>
	</interface>
	"""

	class Anchor(IntFlag):
		TOP = 1
		BOTTOM = 2
		LEFT = 4
		RIGHT = 8

	def __init__(self, con):
		self.object_id = 6
		self.con = con
		self.configured = False
		self.width = 0
		self.height = 0
		self.stride = 0
		self.size = 0
		self.serial = -1

	def handle_event_configure(self, serial, width, height):
		self.configured = True
		self.width = width
		self.height = height
		self.stride = self.width * 4
		self.size = self.stride * self.height
		self.serial = serial
		print(f"Rcvd conf s {serial} w {width}, h {height}!", flush=True)

	def register_event_configure(self):
		opcode = 0
		arg_types = (int, int, int)
		self.con.register_event(
			self.object_id,
			opcode,
			arg_types,
			self.handle_event_configure
		)

	def reqister_request_ack_configure(self, serial):
		opcode = 6
		self.con.register_request(self.object_id, opcode, serial)

	def register_request_set_size(self, width, height):
		opcode = 0
		self.con.register_request(
			self.object_id,
			opcode,
			width,
			height
		)

	def register_request_set_anchor(self, anchor):
		opcode = 1
		self.con.register_request(self.object_id, opcode, anchor)


class WlShm:
	"""
	<interface name="wl_shm" version="1">
		<enum name="error">
			<entry name="invalid_format" value="0" summary="buffer format is not known"/>
			<entry name="invalid_stride" value="1" summary="invalid size or stride during pool or buffer creation"/>
			<entry name="invalid_fd" value="2" summary="mmapping the file descriptor failed"/>
		</enum>

		<enum name="format">
			<!-- The drm format codes match the #defines in drm_fourcc.h.
				The formats actually supported by the compositor will be
				reported by the format event. -->
		</enum>

		<request name="create_pool">
			<arg name="id" type="new_id" interface="wl_shm_pool"/>
			<arg name="fd" type="fd"/>
			<arg name="size" type="int"/>
		</request>

		<event name="format">
			<arg name="format" type="uint" enum="format"/>
		</event>
	</interface>
	"""

	def __init__(self, con):
		self.object_id = 7
		self.con = con
		self.format = -1

	def handle_event_format(self, format):
		print(f"Received format {format}", flush=True)

		if format == 1:
			self.format = 1

	def register_event_format(self):
		opcode = 0
		arg_types = (int, )
		self.con.register_event(
			self.object_id,
			opcode,
			arg_types,
			self.handle_event_format
		)

	def register_request_create_pool(self, new_id, fd, size):
		opcode = 0
		aux = (
			socket.SOL_SOCKET,
			socket.SCM_RIGHTS,
			struct.pack("i", fd)
		)
		self.con.register_request(
			self.object_id,
			opcode,
			new_id,
			size,
			aux=aux
		)


class WlShmPool:
	"""
	<interface name="wl_shm_pool" version="2">
		<request name="create_buffer">
			<arg name="id" type="new_id" interface="wl_buffer" summary="buffer to create"/>
			<arg name="offset" type="int" summary="buffer byte offset within the pool"/>
			<arg name="width" type="int" summary="buffer width, in pixels"/>
			<arg name="height" type="int" summary="buffer height, in pixels"/>
			<arg name="stride" type="int" summary="number of bytes from the beginning of one row to the beginning of the next row"/>
			<arg name="format" type="uint" enum="wl_shm.format" summary="buffer pixel format"/>
		</request>

		<request name="destroy" type="destructor"/>

		<request name="resize">
			<arg name="size" type="int" summary="new size of the pool, in bytes"/>
		</request>
	</interface>
	"""

	def __init__(self, con):
		self.object_id = 8
		self.con = con
		self.buf_fd = -1
		self.buf = None

	def create_shared_frame_buffer(self, size):
		self.buf_fd = os.memfd_create("bg_frame_buffer")
		os.ftruncate(self.buf_fd, size)
		self.buf = mmap.mmap(
			self.buf_fd,
			size,
			flags=mmap.MAP_SHARED,
			prot=mmap.PROT_READ | mmap.PROT_WRITE,
		)

	def register_request_create_buffer(
		self,
		new_id,
		offset,
		width,
		height,
		stride,
		format
	):
		opcode = 0
		self.con.register_request(
			self.object_id,
			opcode,
			new_id,
			offset,
			width,
			height,
			stride,
			format
		)


class WlBuffer:
	"""
	<interface name="wl_buffer" version="1">
		<request name="destroy" type="destructor">
		</request>

		<event name="release">
		</event>
	</interface>

	"""

	def __init__(self, con):
		self.object_id = 9
		self.con = con


class Client:
	class State(Enum):
		NOT_STARTED = 1
		STARTED = 2
		FIRST_SURFACE_COMMIT = 3
		SET_SHM_POOL = 4
		SET_BUFFER = 5
		SET_FIRST_RENDER = 6

	def __init__(self, con):
		self.state = self.State.NOT_STARTED
		self.con = con
		self.display = None
		self.registry = None
		self.compositor = None
		self.surface = None
		self.layer_shell = None
		self.layer_surface = None
		self.shm = None
		self.shm_pool = None
		self.buffer = None

	def run(self):
		if self.state == self.State.NOT_STARTED:
			self.con.connect()
			self.display = WlDisplay(self.con)
			self.registry = WlRegistry(self.con)
			self.registry.register_event_global()
			self.display.register_request_get_registry(
				self.registry.object_id
			)
			self.state = self.State.STARTED
		
		elif self.state == self.State.STARTED:
			comp = self.registry.rcvd_g_events.get("wl_compositor")
			lay_srf = self.registry.rcvd_g_events.get(
				"zwlr_layer_shell_v1"
			)

			if not comp and lay_srf:
				return
			
			self.compositor = WlCompositor(self.con)
			self.registry.register_request_bind(
				comp["name"],
				"wl_compositor",
				comp["version"],
				self.compositor.object_id
			)
			print(f"WlCompositor created", flush=True)

			self.surface = WlSurface(self.con)
			self.compositor.register_request_create_surface(
				self.surface.object_id
			)
			print(f"WlSurface created", flush=True)
			
			self.layer_shell = ZwlrLayerShellV1(self.con)
			self.registry.register_request_bind(
				lay_srf["name"],
				"zwlr_layer_shell_v1",
				lay_srf["version"],
				self.layer_shell.object_id
			)
			print(f"ZwlrLayerShellV1 created", flush=True)

			self.layer_surface = ZwlrLayerSurfaceV1(self.con)
			self.layer_shell.register_request_get_layer_surface(
				self.layer_surface.object_id,
				self.surface.object_id,
				None,
				0,
				"wallpaper"
			)
			print(f"LayerSurface created", flush=True)

			self.layer_surface.register_event_configure()
			self.layer_surface.register_request_set_size(
				0,
				64
			)
			self.layer_surface.register_request_set_anchor(
				self.layer_surface.Anchor.TOP
				| self.layer_surface.Anchor.LEFT
				| self.layer_surface.Anchor.RIGHT
			)
			self.surface.register_request_commit()
			self.state = self.State.FIRST_SURFACE_COMMIT
			print(f"Commited surface", flush=True)
		
		elif self.state == self.State.FIRST_SURFACE_COMMIT:
			if not self.layer_surface.configured:
				return
			
			self.layer_surface.configured = False
			self.layer_surface.reqister_request_ack_configure(
				self.layer_surface.serial
			)
			print("Acked configure", flush=True)

			shm = self.registry.rcvd_g_events.get(
				"wl_shm"
			)
			self.shm = WlShm(self.con)
			self.shm.register_event_format()
			self.registry.register_request_bind(
				shm["name"],
				"wl_shm",
				shm["version"],
				self.shm.object_id
			)
			print(f"Shm created", flush=True)

			self.shm_pool = WlShmPool(self.con)
			self.shm_pool.create_shared_frame_buffer(
				self.layer_surface.size
			)
			self.shm.register_request_create_pool(
				self.shm_pool.object_id,
				self.shm_pool.buf_fd,
				self.layer_surface.size,
			)
			self.state = self.State.SET_SHM_POOL
			print(f"Shm pool with frame buffer created", flush=True)

		elif self.state == self.State.SET_SHM_POOL:
			if not self.shm.format == 1:
				return

			self.buffer = WlBuffer(self.con)
			self.shm_pool.register_request_create_buffer(
				self.buffer.object_id,
				0,
				self.layer_surface.width,
				self.layer_surface.height,
				self.layer_surface.stride,
				self.shm.format
			)
			r = 0xff << 16
			g = 0x00 << 8
			b = 0x00
			pixel = struct.pack("=I", r + g + b)
			num_pixels = (
				self.layer_surface.width
				* self.layer_surface.height
			)
			self.shm_pool.buf[:] = pixel * num_pixels
			self.surface.register_request_attach(
				self.buffer.object_id,
				0,
				0
			)
			self.surface.register_request_commit()
			self.state = self.State.SET_BUFFER
			print(f"Buffer created adn attached", flush=True)

		elif self.state == self.State.SET_BUFFER:
			if not self.layer_surface.configured:
				return

			self.layer_surface.configured = False
			self.layer_surface.reqister_request_ack_configure(
				self.layer_surface.serial
			)
			self.state = self.State.SET_FIRST_RENDER
			print("Acked configure", flush=True)
			

def main():
	client = Client(WlConnection())

	while True:
		client.run()

		if client.con.out_messages:
			client.con.send_messages()

		rlist, _, _ = select.select(
			[client.con.sock_fd],
			[],
			[]
		)
		
		if rlist:
			client.con.receive_messages()

		client.con.dispatch()
		# time.sleep(1)


if __name__ == "__main__":
	main()
