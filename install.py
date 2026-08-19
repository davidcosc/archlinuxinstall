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
		except OSError:
			break
		
		# pipes will perma be readably with EOF so we break on EOF
		if not read_bytes:
			break

		total_read_bytes += read_bytes

	return total_read_bytes.decode("utf-8")


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
		time.sleep(in_delay)

	for i in inputs:
		os.write(mfd, i.encode("utf-8"))
		time.sleep(in_interval)

	return pid, mfd


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


def fail_no_inet():
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


def fail_not_uefi():
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
		"amd-ucode",
		"dosfstools",
		"e2fsprogs",
		"networkmanager",
		"nano",
		"man-db",
		"man-pages",
		"texinfo",
		"base-devel"	
	]
	return config


def umount_partitions(device):
	# only works for running script multiple times not prev arbitrary mounts
	cmds = [
		["/usr/bin/swapoff", "-a"],
		["/usr/bin/umount", "-R", "/mnt"]
	]

	for cmd in cmds:
		subprocess_output(*cmd)
		time.sleep(1)
	

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
		"-nrpo",
		"NAME",
		device
	)
	partitions = result.splitlines()[2:]
	print(partitions)
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


def add_packages(config):
	cmds = [
		["/usr/bin/pacman", "-Sy", "--noconfirm", "archlinux-keyring"],
		["/usr/bin/pacstrap", "-K", "/mnt"] + config["packages"]
	]

	for cmd in cmds:
		ret_code, _ = subprocess_output(*cmd, timeout=10)

		if ret_code != 0:
			exit(1)


def gen_fstab():
	ret_code, result = subprocess_output(
		"/usr/bin/genfstab",
		"-U",
		"/mnt"
	)

	if ret_code != 0:
		exit(1)

	fd = os.open(
		"/mnt/etc/fstab",
		os.O_WRONLY | os.O_CREAT | os.O_APPEND,
		0o644
	)
	os.write(fd, "\n".join(result.splitlines()[1:]).encode("utf-8"))
	os.close(fd)
	subprocess_output("/usr/bin/cat", "/mnt/etc/fstab")


def setup_system():
	pw = get_input(f"Choose password:") + "\n"
	inputs = [
		'ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime\n',
		'echo "en_GB.UTF-8 UTF-8" >> /etc/locale.gen\n',
		'locale-gen\n',
		'echo "LANG=en_GB.UTF-8" > /etc/locale.conf\n',
		'/usr/bin/echo "KEYMAP=de-latin1" > /etc/vconsole.conf\n',
		'/usr/bin/echo "shen" > /etc/hostname\n',
		'systemctl enable NetworkManager\n',
		"passwd\n",
		pw,
		pw,
		'/usr/bin/pacman -Sy --noconfirm grub efibootmgr\n',
		'grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB\n'
	]
	ret_code, _ = subprocess_output(
		"/usr/bin/arch-chroot",
		"/mnt",
		timeout=15,
		inputs=inputs,
		in_delay=3,
		in_interval=3
	)


def do_install():
	fail_no_inet()
	fail_not_uefi()
	config = configure()
	print(config)
	umount_partitions(config["device"])
	partitions = partition(config["device"])
	mount_partitions(partitions)
	add_packages(config)
	gen_fstab()
	setup_system()


if __name__ == "__main__":
	do_install()