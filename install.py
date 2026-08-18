import fcntl
import os
import select
import time


TIOCSCTTY = 0x540E


def subprocess(*cmd):
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


def subprocess_output(*cmd, timeout=1):
	pid, rwfd = subprocess(*cmd)
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
		os.write(1, f"error no inet\n".encode("utf-8"))
		exit(1)


def check_uefi():
	ret_code, _ = subprocess_output(
		"/usr/bin/cat",
		"/sys/firmware/efi/fw_platform_size"
	)
	return ret_code == 0


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
		device = get_input(f"Select device to partion:")

		if device in result:
			break

		os.write(1,f"Device does not exist.\n".encode("utf-8"))

	return device


def configure():
	config = {}
	config["is_uefi"] = check_uefi()
	config["device"] = select_device()

	config["partitions"] = [
		b"label: gpt\n",
		b"size=1GiB, type=uefi\n",
		b"size=4GiB, type=swap\n",
		b"type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709\n"
	] if config["is_uefi"] else [
		b"label: dos\n",
		b"size=4GiB, type=82\n",
		b"type=83, bootable\n"
	]

	config["partition_formats"] = [
		["/usr/bin/mkfs.fat", "-F", "32"],
		["/usr/bin/mkswap"],
		["/usr/bin/mkfs.ext4"]
	] if config["is_uefi"] else [
		["/usr/bin/mkswap"],
		["/usr/bin/mkfs.ext4"]
	]

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


def partition(config):
	pid, rwfd = subprocess(
		"/usr/bin/sfdisk",
		"--wipe-partitions",
		"always",
		config["device"]
	)
	result = read(rwfd, timeout=3)
	print(result)
	
	for part in config["partitions"]:
		os.write(rwfd, part)

	# Tell sfdisk that we're finished entering the script.
	os.write(rwfd, b"\x04") # Ctrl-D
	os.write(rwfd, b"y\n")
	result = read(rwfd, timeout=5)
	print(result)
	os.close(rwfd)
	_, status = os.waitpid(pid, 0)
	ret_code = os.waitstatus_to_exitcode(status)

	if ret_code != 0:
		exit(1)

	_, result = subprocess_output(
		"/usr/bin/lsblk",
		f"{config["device"]}",
		"-o",
		"NAME"
	)
	partitions = [f"/dev/{line[2:]}" for line in result.splitlines()[3:]]
	time.sleep(5)

	for cmd, part in zip(config["partition_formats"], partitions):
		cmd += [part]
		ret_code, _ = subprocess_output(*cmd)
		if ret_code != 0:
			exit(1)

	return partitions


def mount_partitions(config, partitions):
	partitions.reverse()
	ret_code, _ = subprocess_output(
		"/usr/bin/mount",
		partitions[0],
		"/mnt"
	)

	if ret_code != 0:
		exit(1)

	ret_code, _ = subprocess_output(
		"/usr/bin/swapon",
		partitions[1]
	)

	if ret_code != 0:
		exit(1)

	if config["is_uefi"]:
		ret_code, _ = subprocess_output(
			"/usr/bin/mount",
			"--mkdir",
			partitions[2],
			"/mnt/boot"
		)

		if ret_code != 0:
			exit(1)


def do_install():
	check_inet()
	config = configure()
	print(config)
	partitions = partition(config)
	mount_partitions(config, partitions)


if __name__ == "__main__":
	do_install()