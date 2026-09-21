# archlinuxinstall

Custom made from head to toe! WOOOO!

Purpose	Color	Hex
Normal text	Soft light gray	#D6D6D6
Function names	Purple	#C586C0
Function names	Purple	#e18eda
Function names	Purple	#7f1e77
Keywords	Coral/red	#F44747
Function arguments	Gray	#B8B8B8
Strings	Orange	#CE9178
Booleans/constants	Blue	#569CD6
Numbers	Light blue	#B5CEA8
Operators	Light gray	#D4D4D4
Comments	Muted gray	#6A737D



wl_surface.commit()     ← no buffer
       │
       ▼
layer_surface.configure(width, height)
       │
       ├── ack_configure(serial)
       ├── determine buffer scale
       ├── render(width × height × scale)
       ├── attach(buffer)
       ├── damage_buffer(...)
       └── commit()

Then if the user changes resolution:

monitor resolution changes
          │
          ▼
compositor recalculates layer-shell geometry
          │
          ▼
layer_surface.configure(new_width, new_height)
          │
          ▼
ack_configure()
          │
          ▼
render new-size buffer
          │
          ▼
attach + damage + commit

And if the scale changes:

preferred_buffer_scale(new_scale)
          │
          ▼
re-render buffer at new scale
          │
          ▼
set_buffer_scale(new_scale)
          │
          ▼
attach + damage + commit

One important implementation detail: configure and preferred-buffer-scale events can arrive independently, so your state machine should keep the latest values and render using the current combination rather than assuming a particular event ordering.

Also, don't use wl_output.mode.refresh to drive repaint timing. Your supplied protocol explicitly recommends wl_surface.frame (or presentation-time) for that. For a static wallpaper you generally only need to render when the size/scale/content changes anyway.
You said:

So basically I am going to ignore output events and only use global wl_output as event to create a new output. I then create the surface, layer surface and commit for new output after reigstering the relevant listeners. I keep references to the wl_output, surface, layer_surface objects in my output object. I always rerender on configure or preferred buffer scale events. What does the render part look like. On configure I store the current output dimensions in case the scale might change later on i can use them to scale the buffer appropriately. Now the crutial part for me. What does a rerender entail? Do I destroy the old wl_shm_pool and wl_buffer as well as the actuall mmapped buffer and recreate them with the new sizes?
ChatGPT said:

Yes — you generally do not need to recreate the wl_shm_pool/mapping on every rerender. A rerender is normally just:

    Determine the buffer dimensions from the current logical size + buffer scale.

    Render your new pixels into an available wl_buffer.

    wl_surface_attach() the buffer.

    wl_surface_damage_buffer() the region you changed.

    wl_surface_commit().

    Wait for the compositor to release the buffer before reusing it.

The important distinction is rerendering vs. resizing.
Normal rerender

Suppose your output has:

logical size: 1920 × 1080
buffer scale: 2

Your SHM buffer might therefore be:

buffer: 3840 × 2160

On a normal rerender, you don't destroy anything. You simply overwrite the pixels in the mapped memory:

render_into(mapped_pixels, width, height);

wl_surface_attach(surface, buffer, 0, 0);
wl_surface_damage_buffer(surface, 0, 0, width, height);
wl_surface_commit(surface);

Then you must not immediately overwrite that buffer again. The compositor owns the submitted buffer until it sends:

wl_buffer.release

So in practice you usually have at least two SHM buffers and alternate between them:

buffer A ──render──> commit ──> compositor
                                      │
buffer B ──render──> commit ──> compositor
                                      │
buffer A <──────── wl_buffer.release

For a continuously animated surface, triple buffering can also be useful.
When the size/scale changes

This is the case where you potentially need to resize your SHM allocation.

For example:

configure:
    logical size = 1920 × 1080

preferred_buffer_scale:
    scale = 2

You calculate:

buffer_width  = logical_width  * scale;
buffer_height = logical_height * scale;

and allocate enough memory for that.

If later you receive:

preferred_buffer_scale = 1

then you need:

3840 × 2160  →  1920 × 1080

At that point you need a buffer with the new dimensions.

You can destroy the old pool/mapping and create a new one, but there is an important lifetime issue:

    You cannot destroy/reuse the memory backing a wl_buffer while the compositor may still be using that buffer.

So don't do:

commit buffer A
↓
immediately munmap/free pool A
↓
create new buffer

because the compositor may still be reading from A.

Instead, track buffer lifetime through wl_buffer.release.
A useful structure

Something like:

struct shm_buffer {
    struct wl_buffer *wl_buffer;

    void *data;
    size_t size;

    int width;
    int height;
    int stride;

    bool busy;
};

struct output {
    struct wl_output *wl_output;

    struct wl_surface *surface;
    struct zwlr_layer_surface_v1 *layer_surface;

    int logical_width;
    int logical_height;
    int scale;

    struct shm_buffer buffers[2];
};














The output resolution changes rarely. Would it not be simpler to recreate the pool every time, instead of wasting memory that is possible never used if the use never changes resolution?! We are still talking about a wallpaper client not a terminal windo
ChatGPT said:

Yes — for a wallpaper client, recreating the SHM resources on an actual size change is a very reasonable and arguably simpler design.

You don't need to optimize this like a terminal emulator that is constantly resizing.

A simple lifecycle could be:

configure
   │
   ├── new size == current size
   │       └── keep existing pool/buffers
   │
   └── new size
           │
           ├── stop using old buffers
           ├── wait until old buffers are released
           ├── destroy old wl_buffers
           ├── unmap old memory
           ├── destroy old wl_shm_pool
           ├── close old fd
           │
           └── create new pool + mmap + buffers

The one very important caveat is the release issue we discussed.

You can't do this immediately upon receiving a configure if the compositor still has an old buffer in use. You must keep the old pool/memory alive until all buffers from it that have been attached have received wl_buffer.release.

So your output might temporarily look like:

Output
 │
 ├── current resources
 │    ├── pool
 │    ├── buffer A
 │    └── buffer B
 │
 └── retired resources
      └── old pool
           └── old buffer waiting for release



















Your single-buffer model

You can have:

output
 ├── wl_surface
 ├── layer_surface
 └── framebuffer
      ├── mmap
      ├── wl_shm_pool
      └── wl_buffer

Render into it:

draw pixels
    ↓
wl_surface.attach(buffer)
wl_surface.damage(...)
wl_surface.commit()

Then wait for:

wl_buffer.release

At that point the compositor is finished with the buffer and you can render into it again.
Resize

When the layer surface sends a new configure:

configure(new_width, new_height)
        │
        ▼
create new framebuffer
        │
        ├── new fd
        ├── new mmap
        ├── new wl_shm_pool
        └── new wl_buffer
        │
        ▼
ack_configure()
        │
        ▼
render new framebuffer
        │
        ▼
attach + commit

You don't need to inspect the old buffer's size and resize it.

Instead, keep the old framebuffer around until its wl_buffer.release arrives:

current:
    new pool
    new buffer
       │
       │ compositor may be using it
       ▼
old:
    old pool
    old buffer
       │
       │ waiting for release
       ▼
    destroy everything

One caveat

If you have only one buffer, you cannot start drawing your next frame until that buffer has been released.

For a wallpaper that's usually completely fine:

render
  ↓
commit
  ↓
wait for release
  ↓
render again
  ↓
commit
  ↓
wait














es. With your single-buffer design, the flow is essentially:

timer fires
   │
   ▼
buffer is available?
   │
   ▼
modify the mapped pixels
   │
   ▼
wl_surface.damage_buffer(...)
   │
   ▼
wl_surface.commit()
   │
   ▼
wait for wl_buffer.release

For your red/green wallpaper:
Initial render

You create the buffer and fill it with red:

memset(mapped_pixels, RED, buffer_size);

wl_surface_attach(surface, buffer, 0, 0);
wl_surface_damage_buffer(surface, 0, 0, width, height);
wl_surface_commit(surface);

Now the compositor owns/uses that buffer.
30 minutes later

You wait until wl_buffer.release has happened for that buffer.

Then you can safely modify the mapped memory:

fill_pixels(mapped_pixels, GREEN);

Then:

wl_surface_damage_buffer(surface, 0, 0, width, height);
wl_surface_attach(surface, buffer, 0, 0);
wl_surface_commit(surface);














bool buffer_released = false;
bool color_change_pending = false;

When you initially render:

paint red
→ attach
→ damage_buffer
→ commit
→ buffer_released = false

When the compositor sends:

wl_buffer.release

you do:

buffer_released = true;

Then your timer fires after 30 minutes:

30 minutes elapsed?
    │
    ├── no → do nothing
    │
    └── yes
         │
         ├── buffer_released?
         │      │
         │      ├── no → wait
         │      │
         │      └── yes
         │
         ▼
      modify pixels
         ↓
      damage_buffer
         ↓
      commit
         ↓
      buffer_released = false

Or, even cleaner, treat the timer as setting a pending update:

color_change_pending = true;

if (color_change_pending && buffer_released) {
    render_new_color();

    wl_surface_damage_buffer(...);
    wl_surface_attach(...);   // if needed for your commit state
    wl_surface_commit(...);

    buffer_released = false;
    color_change_pending = false;
}