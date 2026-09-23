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
from collections import deque
from pathlib import Path
from PIL import Image


class WaylandBuffer:
	def __init__(self, object_id):
		self.object_id = object_id

	def destroy(self):
		return (self.object_id, 0, (), ())

	def release(self):
		return (self.object_id, 0, ())


class WaylandShmPool:
	def __init__(self, object_id):
		self.object_id = object_id

	def create_buffer(self, id, offset, width, height, stride, format):
		return (
			self.object_id,
			0,
			(id, offset, width, height, stride, format),
			("new_id", "int", "int", "int", "int", "uint")
		)

	def destroy(self):
		return (self.object_id, 1, (), ())


class ZwlrLayerSurfaceV1:
	def __init__(self, object_id):
		self.object_id = object_id

	def set_size(self, width, height):
		return (self.object_id, 0, (width, height), ("uint", "uint"))

	def set_anchor(self, anchor):
		return (self.object_id, 1, (anchor,), ("uint",))

	def ack_configure(self, serial):
		return (self.object_id, 6, (serial,), ("uint",))

	def configure(self):
		return(self.object_id, 0, ("uint", "uint", "uint"))

	def closed(self):
		return(self.object_id, 1, ())


class WaylandSurface:
	def __init__(self, object_id):
		self.object_id = object_id

	def destroy(self):
		return (self.object_id, 0, (), ())

	def attach(self, buffer, x, y):
		return (
			self.object_id,
			1,
			(buffer, x, y),
			("object", "int", "int")
		)

	def damage(self, x, y, width, height):
		return (
			self.object_id,
			2,
			(x, y, width, height),
			("int", "int", "int", "int")
		)

	def commit(self):
		return (self.object_id, 6, (), ())

	def set_buffer_scale(self, scale):
		return (self.object_id, 8, (scale,), ("int",))

	def preferred_buffer_scale(self):
		return (self.object_id, 2, ("int",))


class Output:
	def __init__(self, wl_output, name):
		self.name = name
		self.wl_output = wl_output
		self.surface = None
		self.layer_surface = None
		self.width = 0
		self.height = 0
		self.preferred_buffer_scale = 1
		self.configure_serial = None
		self.render_pending = False


class ZwlrLayerShellV1:
	def __init__(self, object_id):
		self.object_id = object_id

	def get_layer_surface(self, id, surface, output, layer, namespace):
		return (
			self.object_id,
			0,
			(id, surface, output, layer, namespace),
			("new_id", "object", "object", "uint", "string")
		)


class WaylandOutput:
	def __init__(self, object_id):
		self.object_id = object_id


class WaylandShm:
	def __init__(self, object_id):
		self.object_id = object_id

	def create_pool(self, id, fd, size):
		return (
			self.object_id,
			0,
			(id, fd, size),
			("new_id", "fd", "int")
		)


class WaylandCompositor:
	def __init__(self, object_id):
		self.object_id = object_id

	def create_surface(self, id):
		return (self.object_id, 0, (id,), ("new_id",))


class WaylandCallback:
	def __init__(self, object_id):
		self.object_id = object_id

	def done(self):
		return (self.object_id, 0, ("uint",))


class WaylandRegistry:
	def __init__(self, object_id):
		self.object_id = object_id

	def bind(self, name, interface, version, id):
		return (
			self.object_id,
			0,
			(name, interface, version, id),
			("uint", "string", "uint", "new_id")
		)

	def global_(self):
		return (self.object_id, 0, ("uint", "string", "uint"))

	def global_remove(self):
		return (self.object_id, 1, ("uint",))


class WaylandDisplay:
	def __init__(self, object_id):
		self.object_id = object_id

	def sync(self, callback):
		return (self.object_id, 0, (callback,), ("new_id",))

	def get_registry(self, registry):
		return (self.object_id, 1, (registry,), ("new_id",))

	def error(self):
		return (self.object_id, 0, ("object", "uint", "string"))

	def delete_id(self):
		return (self.object_id, 1, ("uint",))


class WaylandConnection:
	def __init__(self):
		self.sock = None
		self.next_object_id = 0
		self.released_object_ids = deque()
		self.listeners = [None] * 20 * 16
		self.display = None
		self.registry = None
		self.callback = None
		self.compositor = None
		self.shm = None
		self.layer_shell = None
		self.bound_globals = False
		self.outputs = []
		self.out_queue = deque()
		self.in_queue = deque()
		self.task_queue = deque()

	def create_object(self, interface):
		if self.released_object_ids:
			object_id = self.released_object_ids.popleft()
		else:
			self.next_object_id += 1
			object_id = self.next_object_id
			if object_id > 0xfeffffff:
				raise RuntimeError("Ran out of object ids")
		return interface(object_id)

	def destroy_object(self, object_id):
		self.released_object_ids.append(object_id)
		for i in range(16):
			self.listeners[(object_id << 4) | i] = None
		for attr in (
			"display",
			"registry",
			"compositor",
			"callback",
			"shm",
			"layer_shell"
		):
			obj = getattr(self, attr)
			if obj and obj.object_id == object_id:
				setattr(self, attr, None)
				return object_id
		for output in self.outputs:
			for attr in ("wl_output", "surface", "layer_surface"):
				obj = getattr(output, attr)
				if obj and obj.object_id == object_id:
					setattr(output, attr, None)
					return object_id
		return object_id
		
	def listen(self, event, callback):
		object_id, opcode, arg_types = event
		index = (object_id << 4) | opcode
		self.listeners[index] = (callback, arg_types)
		return index

	def pad4(self, n):
		return (4 - (n % 4)) % 4

	def encode_arg(self, arg_type, value):
		if value is None:
			return struct.pack("=I", 0)
		if arg_type == "string":
			data = value.encode("utf-8") + b"\x00"
			len_data = len(data)
			data = data + b"\x00" * self.pad4(len_data)
			return struct.pack("=I", len_data) + data
		return struct.pack("=I", value)

	def enqueue_out_message(self, request, post_request=()):
		object_id, opcode, args, arg_types = request
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
		encoded_args = b"".join(
			self.encode_arg(k, v) for k, v in type_arg_tuples
		)
		size = 8 + len(encoded_args)
		header = struct.pack("=II", object_id, (size << 16) | opcode)
		encoded_msg = header + encoded_args
		print(f"C -> S: {encoded_msg.hex()}", flush=True)
		msg = (encoded_msg, aux, post_request)
		self.out_queue.append(msg)
		return msg

	def decode_args(self, args, arg_types):
		offset = 0
		decoded_args = []
		for arg_type in arg_types:
			first_int = struct.unpack(
				"=I",
				args[offset:offset + 4]
			)[0]
			offset += 4
			if arg_type == "string":
				slen = first_int
				sbytes = args[offset:offset + slen]
				decoded_args.append(sbytes[:-1].decode("utf-8"))
				offset += slen + self.pad4(slen)
			else:
				arg = first_int
				decoded_args.append(arg)
		return decoded_args

	def enqueue_in_messages(self, data):
		offset = 0
		while offset < len(data):
			object_id, size_opcode = struct.unpack(
				"=II",
				data[offset:offset + 8]
			)
			size = size_opcode >> 16
			opcode = size_opcode & 0xffff
			args = data[offset + 8:offset + size]
			print(
				f"S -> C: {data[offset:offset + size].hex()}",
				flush=True
			)
			offset += size
			listener = self.listeners[(object_id << 4) | opcode]
			if not listener:
				continue
			callback, arg_types = listener
			decoded_args = self.decode_args(args, arg_types)
			self.in_queue.append(
				(object_id, callback, decoded_args)
			)

	def connect(self):
		# https://wayland-book.com/protocol-design/wire-protocol.html
		# #transports
		# we do not check WAYLAND_SOCKET since this client is not 
		# intended to be used as a subclient
		runtime_dir = os.environ["XDG_RUNTIME_DIR"]

		if not runtime_dir:
			exit(1)
		
		display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
		path = os.path.join(runtime_dir, display)
		sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		sock.connect(path)
		self.sock = sock

	def flush_out_queue(self):
		while self.out_queue:
			data, aux, post_request = self.out_queue.popleft()
			auxdata = [aux] if aux else []
			num_sent = 0
			while num_sent < len(data):
				num_sent += self.sock.sendmsg(
					[data[num_sent:]],
					auxdata if num_sent == 0 else []
				)
			if post_request:
				func, args = post_request
				func(*args)

	def fill_in_queue(self):
		data = os.read(self.sock.fileno(), 4096)
		if data == b"":
			raise ConnectionError("Compositor closed connection")
		self.enqueue_in_messages(data)

	def dispatch(self):
		while self.in_queue:
			object_id, callback, args = self.in_queue.popleft()
			callback(self, object_id, *args)

	def work_tasks(self):
		while self.task_queue:
			task, args = self.task_queue.popleft()
			task(self, *args)

# ------------------------------------------------------------------------------
# OUTPUT HANDLING
# ------------------------------------------------------------------------------
def destroy_shared_memory(buf, buf_fd):
	print(f"Destroy shared memory: {buf_fd}", flush=True)
	os.close(buf_fd)
	buf.close()


def render_output(wl_connection, output):
	print(f"Output: Start render", flush=True)
	output.render_pending = False
	stride = output.width * 4
	size = stride * output.height
	buf_fd = os.memfd_create("bg_frame_buffer")
	os.ftruncate(buf_fd, size)
	buf = mmap.mmap(
		buf_fd,
		size,
		flags=mmap.MAP_SHARED,
		prot=mmap.PROT_READ | mmap.PROT_WRITE,
	)
	shm_pool = wl_connection.create_object(WaylandShmPool)
	print(f"Output: Create wl_shm_pool {shm_pool.object_id}", flush=True)
	wl_connection.enqueue_out_message(
		wl_connection.shm.create_pool(shm_pool.object_id, buf_fd, size)
	)
	wl_buf = wl_connection.create_object(WaylandBuffer)
	print(f"Output: Create wl_buffer {wl_buf.object_id}", flush=True)
	wl_connection.enqueue_out_message(
		shm_pool.create_buffer(
			wl_buf.object_id,
			0,
			output.width,
			output.height,
			stride,
			1
		)
	)
	buf[:] = b"\x00\xff\x00\x00" * (output.width * output.height)
	wl_connection.enqueue_out_message(
		output.surface.attach(wl_buf.object_id, 0, 0)
	)
	wl_connection.enqueue_out_message(
		output.surface.damage(0, 0, output.width, output.height)
	)
	wl_connection.enqueue_out_message(output.surface.commit())
	wl_connection.enqueue_out_message(wl_buf.destroy())
	wl_connection.enqueue_out_message(
		shm_pool.destroy(),
		post_request=(destroy_shared_memory, (buf, buf_fd))
	)


def handle_new_output(wl_connection, output):
	output.surface = wl_connection.create_object(WaylandSurface)
	wl_connection.listen(
		output.surface.preferred_buffer_scale(),
		on_scale
	)
	wl_connection.enqueue_out_message(
		wl_connection.compositor.create_surface(
			output.surface.object_id
		)
	)
	print(f"Output: Create surface {output.surface.object_id}", flush=True)
	output.layer_surface = (
		wl_connection.create_object(ZwlrLayerSurfaceV1)
	)
	wl_connection.listen(
		output.layer_surface.configure(),
		on_configure
	)
	wl_connection.listen(
		output.layer_surface.closed(),
		on_closed
	)
	wl_connection.enqueue_out_message(
		wl_connection.layer_shell.get_layer_surface(
			output.layer_surface.object_id,
			output.surface.object_id,
			output.wl_output.object_id,
			0,
			"bg_wallpaper"
		)
	)
	print(
		f"Output: Create layer surface"
		+ f" {output.layer_surface.object_id}",
		flush=True
	)
	wl_connection.enqueue_out_message(
		output.layer_surface.set_size(0, 0)
	)
	wl_connection.enqueue_out_message(
		output.layer_surface.set_anchor(1 | 2 | 4 | 8)
	)
	wl_connection.enqueue_out_message(output.surface.commit())


# ------------------------------------------------------------------------------
# ROUTING AND EVENT HANDLING
# ------------------------------------------------------------------------------
def on_closed(wl_connection, ref_object_id):
	print(f"Closed: {ref_object_id}", flush=True)


def on_configure(wl_connection, ref_object_id, serial, width, height):
	print(f"Configure: {serial} {width} {height}", flush=True)
	output = None
	for outp in wl_connection.outputs:
		if outp.layer_surface.object_id == ref_object_id:
			output = outp
			break
	output.width = width
	output.height = height
	output.configure_serial = serial
	wl_connection.enqueue_out_message(
		output.layer_surface.ack_configure(serial)
	)
	if not output.render_pending:
		output.render_pending = True
		wl_connection.task_queue.append(
			(render_output, (output,))
		)


def on_scale(wl_connection, ref_object_id, scale):
	output = None
	for outp in wl_connection.outputs:
		if outp.surface.object_id == ref_object_id:
			output = outp
			break
	output.preferred_buffer_scale = scale
	if not output.render_pending:
		output.render_pending = True
		wl_connection.task_queue.append(
			(render_output, (output,))
		)
	print(f"Preferred buffer scale: {scale}", flush=True)


def on_error(wl_connection, ref_object_id, object_id, code, message):
	print(f"Error: {code}: {object_id} {message}", flush=True)


def on_delete(wl_connection, ref_object_id, id):
	print(f"Delete: {id}", flush=True)
	wl_connection.destroy_object(id)


def on_global(wl_connection, ref_object_id, name, interface, version):
	wl_object = None
	if interface == "wl_compositor":
		wl_object = wl_connection.create_object(WaylandCompositor)
		wl_connection.compositor = wl_object
	elif interface == "wl_shm":
		wl_object = wl_connection.create_object(WaylandShm)
		wl_connection.shm = wl_object
	elif interface == "wl_output":
		wl_object = wl_connection.create_object(WaylandOutput)
		wl_connection.outputs.append(Output(wl_object, name))
		if wl_connection.bound_globals:
			handle_new_output(
				wl_connection,
				wl_connection.outputs[-1]
			)
	elif interface == "zwlr_layer_shell_v1":
		wl_object = wl_connection.create_object(ZwlrLayerShellV1)
		wl_connection.layer_shell = wl_object
	if wl_object:
		print(
			f"Global: {name} {interface} {version}"
			+ f" bound {wl_object.object_id}",
			flush=True
		)
		wl_connection.enqueue_out_message(
			wl_connection.registry.bind(
				name,
				interface,
				version,
				wl_object.object_id
			)
		)

def on_done(wl_connection, ref_object_id, callback_data):
	print(f"Done: {callback_data}", flush=True)
	wl_connection.bound_globals = True
	for output in wl_connection.outputs:
		handle_new_output(wl_connection, output)



# ------------------------------------------------------------------------------
# EVENT LOOP
# ------------------------------------------------------------------------------
def main():
	wl_connection = WaylandConnection()
	wl_connection.connect()
	wl_display = wl_connection.create_object(WaylandDisplay)
	wl_connection.display = wl_display
	wl_connection.listen(wl_display.error(), on_error)
	wl_connection.listen(wl_display.delete_id(), on_delete)
	wl_registry = wl_connection.create_object(WaylandRegistry)
	wl_connection.registry = wl_registry
	wl_connection.listen(wl_registry.global_(), on_global)
	wl_connection.enqueue_out_message(
		wl_display.get_registry(wl_registry.object_id)
	)
	wl_callback = wl_connection.create_object(WaylandCallback)
	wl_connection.callback = wl_callback
	wl_connection.listen(wl_callback.done(), on_done)
	wl_connection.enqueue_out_message(
		wl_display.sync(wl_callback.object_id)
	)
	while True:
		if wl_connection.task_queue:
			wl_connection.work_tasks()
		if wl_connection.out_queue:
			wl_connection.flush_out_queue()
		rlist, _, _ = select.select(
			[wl_connection.sock.fileno()],
			[],
			[]
		)
		if rlist:
			wl_connection.fill_in_queue()
			wl_connection.dispatch()


if __name__ == "__main__":
	main()