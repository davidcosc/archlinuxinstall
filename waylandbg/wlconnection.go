package main

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

type wlConnection struct {
	sockFd int
}

func wlConnectionConnect(con *wlConnection) {
	// We do not do inherited wayland sockets around here!
	// We do not check WAYLAND_SOCKET env var.
	display := os.Getenv("WAYLAND_DISPLAY")
	if display == "" {
		display = "wayland-0"
	}

	path := display
	if !filepath.IsAbs(display) {
		runtimeDir := os.Getenv("XDG_RUNTIME_DIR")
		if runtimeDir == "" {
			panic(fmt.Errorf("XDG_RUNTIME_DIR is unset"))
		}
		path = filepath.Join(runtimeDir, display)
	}

	fd, err := syscall.Socket(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		panic(err)
	}

	err = syscall.Connect(fd, &syscall.SockaddrUnix{Name: path})
	if err != nil {
		syscall.Close(fd)
		panic(err)
	}

	con.sockFd = fd
	fmt.Printf("Connected to compositor on %d\n", fd)
}
