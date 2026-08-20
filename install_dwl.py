import fcntl
import os
import select
import time


TIOCSCTTY = 0x540E


def read(rfd, timeout=1):
	total_read_bytes = b""

	while True:
		# we want to at most wait 1s for something to read
		rlist, _, _ = select.select([rfd], [], [], timeout)

		if not rlist:
			break

		# some EOF signalled via OSError and might also perma readable
		try:
			read_bytes = os.read(rfd, 1024)
			print(read_bytes.decode("utf-8"), end="")
		except OSError:
			break
		
		# pipes will perma be readably with EOF so we break on EOF
		if not read_bytes:
			break

		total_read_bytes += read_bytes

	return total_read_bytes.decode("utf-8")


def subprocess_output(*cmd, cmd_rtimeout=1, inputs=[], in_rtimeout=1, in_interval=0):
	output = ""
	mfd, sfd = os.openpty()
	pid = os.fork()

	if pid < 0:
		os.write(2, f"error fork".encode("utf-8"))
		exit(1)

	if pid == 0:
		os.close(mfd)
		os.setsid() # login tty
		fcntl.ioctl(sfd, TIOCSCTTY, 0) # login tty
		os.dup2(sfd, 0)
		os.dup2(sfd, 1)
		os.dup2(sfd, 2)
		os.close(sfd)
		pretty_cmd = f"root@archiso ~ # {" ".join(list(cmd))}"
		os.write(0, f"{pretty_cmd}\n".encode("utf-8"))
		os.execv(list(cmd)[0], list(cmd))

	os.close(sfd)

	cmd_output = read(mfd, timeout=cmd_rtimeout)
	# print(cmd_output)
	output += cmd_output

	for i in inputs:
		os.write(mfd, i.encode("utf-8"))
		in_output = read(mfd, timeout=in_rtimeout)
		# print(in_output)
		output += in_output
		time.sleep(in_interval)

	os.close(mfd)

	_, status = os.waitpid(pid, 0)
	return os.waitstatus_to_exitcode(status), output


def do_install():
	cmd = [
		"/usr/bin/pacman",
		"-Sy",
		"--noconfirm",
		"mesa",
		"vulkan-intel",
		"libva-intel-driver",
		"wayland",
		"wayland-protocols",
		"foot",
		"wmenu",
		"git"
	]
	ret_code, _ = subprocess_output(*cmd, cmd_rtimeout=3)

	if ret_code != 0:
		exit()


if __name__ == "__main__":
	do_install()