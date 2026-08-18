import fcntl
import os
import select
import time


TIOCSCTTY = 0x540E


def subprocess(*cmd, inputs=[], in_delay=0, in_interval=0):
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

	if in_delay:
		os.write(0, b"in_delay\n")
		time.sleep(in_delay)

	for i in inputs:
		os.write(mfd, i.encode("utf-8"))
		time.sleep(in_interval)

	return pid, mfd


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
		except OSError:
			break
		
		# pipes will perma be readably with EOF so we break on EOF
		if not read_bytes:
			break

		total_read_bytes += read_bytes

	return total_read_bytes.decode("utf-8")


def subprocess_output(*cmd, timeout=1, inputs=[], in_delay=0, in_interval=0):
	pid, rwfd = subprocess(
		*cmd,
		inputs=inputs,
		in_delay=in_delay,
		in_interval=in_interval
	)
	result = read(rwfd, timeout)
	print(result)
	os.close(rwfd)

	_, status = os.waitpid(pid, 0)
	return os.waitstatus_to_exitcode(status), result


def check_inet():
	ret_code, _ = subprocess_output(
		"/usr/bin/ping",
		"-c",
		"3",
		"8.8.8.8",
		timeout=2
	)
	
	if ret_code != 0:
		os.write(2, f"error no inet\n".encode("utf-8"))
		exit(1)


def check_uefi():
	ret_code, _ = subprocess_output(
		"/usr/bin/cat",
		"/sys/firmware/efi/fw_platform_size"
	)
	if ret_code != 0:
		os.write(2, f"error not a uefi system\n".encode("utf-8"))
		exit(1)


def get_input(prompt):
	os.write(1, prompt.encode("utf-8") + b"\n")
	data = b""

	while True:
		chunk = os.read(0, 1)

		if chunk == b"\n":
			break

		data += chunk

	return data.decode("utf-8")


def select_device():
	subprocess_output("/usr/bin/lsblk")
	_, result = subprocess_output("/usr/bin/fdisk", "-l")

	while True:
		device = get_input(f"Select device to partition:")

		if device in result:
			break

		os.write(1,f"Device does not exist.\n".encode("utf-8"))

	return device


def configure():
	config = {}
	config["device"] = select_device()
	config["packages"] = [
		"base",
		"linux",
		"linux-firmware",
		"linux-firmware-marvell",
		"linux-headers",
		"wireless-regdb",
		"intel-ucode",
		"amd-ucode"
	]
	return config


def partition(device):
	# Ctrl-D \x04 to signal finished and y to write
	partitions = [
		"label: gpt\n",
		"size=1GiB, type=uefi\n",
		"size=4GiB, type=swap\n",
		"type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709\n",
		"\x04",
		"y\n"
	]
	ret_code, _ = subprocess_output(
		"/usr/bin/sfdisk",
		"--wipe-partitions",
		"always",
		device,
		timeout=1,
		inputs=partitions,
		in_delay=1,
		in_interval=1
	)

	if ret_code != 0:
		exit(1)

	_, result = subprocess_output(
		"/usr/bin/lsblk",
		device,
		"-o",
		"NAME"
	)
	partitions = [f"/dev/{line[2:]}" for line in result.splitlines()[3:]]
	time.sleep(5)
	cmds = [
		["/usr/bin/mkfs.fat", "-F", "32", partitions[0]],
		["/usr/bin/mkswap", partitions[1]],
		["/usr/bin/mkfs.ext4", partitions[2]]
	]

	for cmd in cmds:
		ret_code, _ = subprocess_output(*cmd)
		if ret_code != 0:
			exit(1)

	return partitions


def mount_partitions(partitions):
	partitions.reverse()
	cmds = [
		["/usr/bin/mount", partitions[0], "/mnt"],
		["/usr/bin/swapon", partitions[1]],
		["/usr/bin/mount", "--mkdir", partitions[2], "/mnt/boot"]
	]

	for cmd in cmds:
		ret_code, _ = subprocess_output(*cmd)

		if ret_code != 0:
			exit(1)


def do_install():
	check_inet()
	check_uefi()
	config = configure()
	print(config)
	partitions = partition(config["device"])
	mount_partitions(partitions)


if __name__ == "__main__":
	do_install()