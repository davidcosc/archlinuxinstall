import fcntl
import os
import select
import time
from pathlib import Path


GNOME_CONFIG = """\
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-timeout=uint32 0
sleep-inactive-battery-timeout=uint32 0
power-button-action='poweroff'

[org/gnome/desktop/peripherals/touchpad]
send-events='disabled'

[org/gnome/desktop/peripherals/mouse]
accel-profile='flat'

[org/gnome/mutter]
dynamic-workspaces=false

[org/gnome/desktop/wm/preferences]
num-workspaces=1

[org/gnome/desktop/input-sources]
sources=[('xkb', 'de')]
current=uint32 0
"""

GNOME_LOCKS = """\
/org/gnome/desktop/session/idle-delay
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-timeout
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-timeout
/org/gnome/settings-daemon/plugins/power/power-button-action
/org/gnome/desktop/peripherals/touchpad/send-events
/org/gnome/desktop/peripherals/mouse/accel-profile
/org/gnome/mutter/dynamic-workspaces
/org/gnome/desktop/wm/preferences/num-workspaces
/org/gnome/desktop/input-sources/sources
/org/gnome/desktop/input-sources/current
"""

GNOME_PROFILE = """\
user-db:user
system-db:ibus
"""

TIOCSCTTY = 0x540E


def read(rfd, timeout=1):
	total_read_bytes = b""

	while True:
		rlist, _, _ = select.select([rfd], [], [], timeout)

		if not rlist:
			break

		# some EOF signalled via OSError and might also perma readable
		try:
			read_bytes = os.read(rfd, 1024)
			os.write(1, read_bytes)
		except OSError:
			break
		
		# fds might return EOF continuously so we break on EOF
		if not read_bytes:
			break

		total_read_bytes += read_bytes

	return total_read_bytes.decode("utf-8")


def subprocess_output(*cmd, cmd_rtimeout=1, inputs=[], in_rtimeout=1):
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
	output += cmd_output

	for i in inputs:
		os.write(mfd, i.encode("utf-8"))
		in_output = read(mfd, timeout=in_rtimeout)
		output += in_output

	_, status = os.waitpid(pid, 0)
	os.close(mfd)
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
		in_rtimeout=2
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
	os.write(1, str(partitions).encode("utf-8") + b"\n")
	time.sleep(5)
	cmds = [
		["/usr/bin/mkfs.fat", "-F", "32", partitions[0]],
		["/usr/bin/mkswap", partitions[1]],
		["/usr/bin/mkfs.ext4", partitions[2]]
	]

	for cmd in cmds:
		ret_code, _ = subprocess_output(*cmd, cmd_rtimeout=15)
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


def add_packages():
	packages = [
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
		"base-devel",
		"git"	
	]
	cmds = [
		["/usr/bin/pacman", "-Sy", "--noconfirm", "archlinux-keyring"],
		["/usr/bin/pacstrap", "-K", "/mnt"] + packages
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


def setup_base_system():
	user = get_input(f"Choose user name:") +"\n"
	pw = get_input(f"Choose password:") + "\n"
	inputs = [
		'set -e\n',
		"""trap 'echo "FAILED: $BASH_COMMAND"' ERR\n""",
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
		f'useradd -m -g users -G wheel {user}\n',
		f'passwd {user}\n',
		pw,
		pw,
		'/usr/bin/pacman -Syu --noconfirm\n',
		'/usr/bin/pacman -S --noconfirm grub efibootmgr\n',
		'grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB\n',
		'grub-mkconfig -o /boot/grub/grub.cfg\n',
		'/usr/bin/pacman -S --noconfirm sudo\n',
		'exit\n'
	]
	ret_code, _ = subprocess_output(
		"/usr/bin/arch-chroot",
		"/mnt",
		cmd_rtimeout=5,
		inputs=inputs,
		in_rtimeout=5
	)

	if ret_code != 0:
		exit(1)


def configure_gnome():
	subprocess_output("/usr/bin/lspci", "-vnnd", "::03xx")
	prompt = (
		"Install userland graphics libraries:\n"
		"1) VMware i.e. 15ad:0405\n"
		"2) Intel i.e. 8086:*\n"
		"3) AMD i.e. 1002:*\n"
		"4) NVIDIA i.e. 10de:*\n"
	)
	result = get_input(prompt)

	match result:
		case "1":
			packages = ["mesa"]
		case "2":
			packages = ["mesa", "vulkan-intel", "intel-media-driver"]
		case "3":
			packages = ["mesa", "vulkan-radeon"]
		case "4":
			packages = ["nvidia-utils"]

	packages += [
		"pipewire-jack",
		"gnu-free-fonts",
		"firefox",
		"gnome-shell",
		"gnome-session",
		"gdm",
		"gnome-control-center",
		"nautilus",
		"gnome-terminal"
	]

	ret_code, _ = subprocess_output(
		"/usr/bin/pacman",
		"-S",
		"--noconfirm",
		"--needed",
		*packages,
		cmd_rtimeout=10
	)

	if ret_code != 0:
		exit(1)

	ret_code, _ = subprocess_output(
		"/usr/bin/systemctl",
		"enable",
		"gdm"
	)

	if ret_code != 0:
		exit(1)

	config_path = Path("/etc/dconf/db/ibus.d/01-custom-settings")
	locks_path = Path("/etc/dconf/db/ibus.d/locks/01-custom-settings")
	profile_path = Path("/etc/dconf/profile/user")

	config_path.parent.mkdir(parents=True, exist_ok=True)
	locks_path.parent.mkdir(parents=True, exist_ok=True)
	profile_path.parent.mkdir(parents=True, exist_ok=True)

	config_path.write_text(GNOME_CONFIG)
	locks_path.write_text(GNOME_LOCKS)
	profile_path.write_text(GNOME_PROFILE)

	ret_code, _ = subprocess_output("/usr/bin/dconf", "update")

	if ret_code != 0:
		exit(1)

	desktop_config_dir = Path("/usr/local/share/applications")
	desktop_config_dir.mkdir(parents=True, exist_ok=True)

	desktop_files = [
		"avahi-discover.desktop",
		"bssh.desktop",
		"bvnc.desktop",
		"gnome-wellbeing-panel.desktop",
		"lstopo.desktop",
		"qv4l2.desktop",
		"qvidcap.desktop",
	]

	for desktop_file in desktop_files:
		subprocess_output(
			"/usr/bin/cp",
			f"/usr/share/applications/{desktop_file}",
			f"/usr/local/share/applications/{desktop_file}",
			cmd_rtimeout=3
		)
		_, result = subprocess_output(
			"/usr/bin/bash",
			cmd_rtimeout=3,
			inputs=[
				f"echo 'NoDisplay=true' >> /usr/local/share/applications/{desktop_file}\n",
				f"cat /usr/local/share/applications/{desktop_file}\n",
				"exit\n"
			],
			in_rtimeout=3
		)

		if "NoDisplay" not in result:
			print("err append")
			exit(1)


def setup_niri():
	subprocess_output("/usr/bin/lspci", "-vnnd", "::03xx")
	prompt = (
		"Install userland graphics libraries:\n"
		"1) VMware i.e. 15ad:0405\n"
		"2) Intel i.e. 8086:*\n"
		"3) AMD i.e. 1002:*\n"
		"4) NVIDIA i.e. 10de:*\n"
	)
	result = get_input(prompt)

	match result:
		case "1":
			packages = ["mesa"]
		case "2":
			packages = ["mesa", "vulkan-intel", "intel-media-driver"]
		case "3":
			packages = ["mesa", "vulkan-radeon"]
		case "4":
			packages = ["nvidia-utils"]

	packages += [
		"pipewire-jack",
		"gnu-free-fonts",
		"firefox",
		"greetd",
		"greetd-tuigreet",
		"niri",
		"alacritty",
		"fuzzel",
		"wiremix"
	]

	ret_code, _ = subprocess_output(
		"/usr/bin/pacman",
		"-S",
		"--noconfirm",
		"--needed",
		*packages,
		cmd_rtimeout=10
	)

	if ret_code != 0:
		exit(1)

	users = [p for p in Path("/home").iterdir() if p.is_dir()]
	print(users, flush=True)

	if len(users) != 1:
		exit(1)

	user = users[0].name
	greetd_config_path = Path("/etc/greetd/config.toml")
	greetd_config_path.parent.mkdir(parents=True, exist_ok=True)
	niri_config_path = Path(f"/home/{user}/.config/niri/config.kdl")
	niri_config_path.parent.mkdir(parents=True, exist_ok=True)
	alacritty_config_path = Path(
		f"/home/{user}/.config/alacritty/alacritty.toml"
	)
	alacritty_config_path.parent.mkdir(parents=True, exist_ok=True)
	fuzzel_config_path = Path(f"/home/{user}/.config/fuzzel/fuzzel.ini")
	fuzzel_config_path.parent.mkdir(parents=True, exist_ok=True)
	network_manager_tui_desktop_path = Path(
		f"/home/{user}/.local/share/applications/nmtui.desktop"
	)
	network_manager_tui_desktop_path.parent.mkdir(
		parents=True,
		exist_ok=True
	)

	cmds = [
		["/usr/bin/cp", "./greetd.toml", str(greetd_config_path)],
		["/usr/bin/cp", "./niri.kdl", str(niri_config_path)],
		["/usr/bin/cp", "./alacritty.toml", str(alacritty_config_path)],
		["/usr/bin/cp", "./fuzzel.ini", str(fuzzel_config_path)],
		[
			"/usr/bin/cp",
			"./nmtui.desktop",
			str(network_manager_tui_desktop_path)
		],
		[
			"/usr/bin/cp",
			"./wl_wallpaper_client.py",
			"/usr/bin/wallpaper_client"
		],
		["/usr/bin/cp", "./wall1.jpg", f"/home/{user}/wall1.jpg"],
		["/usr/bin/chmod", "755", "/usr/bin/wallpaper_client"],
		["/usr/bin/systemctl", "enable", "greetd"]
	]

	for cmd in cmds:
		ret_code, _ = subprocess_output(*cmd, cmd_rtimeout=5)

		if ret_code != 0:
			exit(1)


def do_install():
	ret_code, _ = subprocess_output("/usr/bin/ls", "/run/archiso")

	if ret_code == 0:
		fail_no_inet()
		fail_not_uefi()
		device = select_device()
		subprocess_output("/usr/bin/umount", "-R", "/mnt", cmd_rtimeout=5)
		subprocess_output("/usr/bin/swapoff", "-a", cmd_rtimeout=5)
		partitions = partition(device)
		mount_partitions(partitions)
		add_packages()
		gen_fstab()
		setup_base_system()
		subprocess_output("/usr/bin/umount", "-R", "/mnt", cmd_rtimeout=5)
	else:
		# configure_gnome()
		setup_niri()


if __name__ == "__main__":
	do_install()