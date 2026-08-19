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


def fail_no_inet():
	ret_code, _ = subprocess_output(
		"/usr/bin/ping",
		"-c",
		"3",
		"8.8.8.8",
		cmd_rtimeout=2
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
		cmd_rtimeout=2,
		inputs=partitions,
		in_rtimeout=2,
		in_interval=0
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
		ret_code, _ = subprocess_output(*cmd, cmd_rtimeout=10)

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
		'grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB\n',
		'grub-mkconfig -o /boot/grub/grub.cfg\n',
		'exit\n'
	]
	ret_code, _ = subprocess_output(
		"/usr/bin/arch-chroot",
		"/mnt",
		cmd_rtimeout=5,
		inputs=inputs,
		in_rtimeout=5,
		in_interval=0
	)


def do_install():
	fail_no_inet()
	fail_not_uefi()
	config = configure()
	print(config)
	subprocess_output("/usr/bin/umount", "-R", "/mnt", cmd_rtimeout=5)
	subprocess_output("/usr/bin/swapoff", "-a", cmd_rtimeout=5)
	partitions = partition(config["device"])
	mount_partitions(partitions)
	add_packages(config)
	gen_fstab()
	setup_system()
	subprocess_output("/usr/bin/umount", "-R", "/mnt", cmd_rtimeout=5)


if __name__ == "__main__":
	do_install()