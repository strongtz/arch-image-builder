import os
import sys
import shutil
from logging import getLogger
from builder.disk import image
from builder.build import mount, fstab, grub, user, filesystem, script
from builder.build import extlinux, systemd_boot
from builder.build import locale, systemd, mkinitcpio, names
from builder.build import pacman as pacman_build
from builder.component import pacman as pacman_comp
from builder.lib.context import ArchBuilderContext
from builder.lib.mount import MountTab
log = getLogger(__name__)


def cleanup(ctx: ArchBuilderContext):
	"""
	Cleanup unneeded files for Arch Linux
	"""
	root = ctx.get_rootfs()

	def rm_rf(path: str):
		real = os.path.join(root, path)
		if not os.path.exists(real): return
		if os.path.isdir(real):
			shutil.rmtree(real, True)
		else:
			os.remove(real)

	def del_child(path: str, prefix: str = None, suffix: str = None):
		real = os.path.join(root, path)
		if not os.path.exists(real): return
		for file in os.listdir(real):
			if prefix and not file.startswith(prefix): continue
			if suffix and not file.endswith(suffix): continue
			rm_rf(os.path.join(real, file))
	rm_rf("var/log/pacman.log")
	del_child("var/cache/pacman/pkg")
	del_child("var/lib/pacman/sync")
	del_child("etc", suffix="-")
	rm_rf("etc/.pwd.lock")


def do_copy(ctx: ArchBuilderContext, src: str, dst: str):
	"""
	Copying rootfs via rsync
	"""
	rsrc = os.path.realpath(src)
	rdst = os.path.realpath(dst)
	log.info("start copying rootfs...")
	args = ["rsync", "--archive", "--recursive", "--delete"]
	if os.isatty(sys.stdout.fileno()):
		args.append("--info=progress2")
	args.append(rsrc + os.sep)
	args.append(rdst)
	ret = ctx.run_external(args)
	os.sync()
	if ret != 0: raise OSError("rsync failed")


def remove_workspace(ctx: ArchBuilderContext):
	# ensure mount point is clean
	mnts = MountTab.parse_mounts()
	if any(mnts.find_folder(ctx.work)):
		raise RuntimeError("mount points not cleanup")

	# DANGEROUS: fully remove workspace
	log.info(f"cleaning workspace {ctx.work}")
	shutil.rmtree(ctx.work)


def build_rootfs(ctx: ArchBuilderContext):
	"""
	Build whole rootfs and generate image
	"""
	log.info("building rootfs")

	# clean workspace
	if ctx.clean and os.path.exists(ctx.work):
		script.run_scripts(ctx, "pre-clean")
		remove_workspace(ctx)
		script.run_scripts(ctx, "after-clean")

	# create folders
	os.makedirs(ctx.work, mode=0o755, exist_ok=True)
	os.makedirs(ctx.get_rootfs(), mode=0o0755, exist_ok=True)
	os.makedirs(ctx.get_output(), mode=0o0755, exist_ok=True)
	os.makedirs(ctx.get_mount(), mode=0o0755, exist_ok=True)
	script.run_scripts(ctx, "start")

	# build rootfs contents
	if not ctx.repack:
		try:
			# running scripts hooks (for environment init)
			script.run_scripts(ctx, "pre-init")

			# initialize basic folders
			mount.init_rootfs(ctx)

			# initialize mount points for chroot
			mount.init_mount(ctx)

			# running add files hooks (for build settings)
			filesystem.add_files_all(ctx, "pre-build")

			# running scripts hooks (for build settings)
			script.run_scripts(ctx, "pre-build")

			# initialize pacman context
			pacman = pacman_comp.Pacman(ctx)

			# initialize build time keyring
			pacman.init_keyring()

			# trust pgp key in config (for pacman database, allow failed)
			pacman_build.trust_all(ctx, pacman, True)

			# update pacman repos databases
			pacman.load_databases()

			# install all keyring packages before other packages
			pacman_build.proc_pacman_keyring(ctx, pacman)

			# trust pgp key in config
			pacman_build.trust_all(ctx, pacman)

			# running add files hooks (for pacman settings)
			filesystem.add_files_all(ctx, "pre-pacman")

			# running scripts hooks (for pacman settings)
			script.run_scripts(ctx, "pre-pacman")

			# real install all packages
			pacman_build.proc_pacman(ctx, pacman)

			# reload user databases after install packages
			ctx.reload_passwd()

			# running add files hooks (for user settings)
			filesystem.add_files_all(ctx, "pre-user")

			# running scripts hooks (for user settings)
			script.run_scripts(ctx, "pre-user")

			# create custom users and groups
			user.proc_usergroup(ctx)

			# build time files add/remove hooks
			filesystem.proc_filesystem(ctx)

			# enable / disable systemd units
			systemd.proc_systemd(ctx)

			# setup locale (timezone / i18n language / fonts / input methods)
			locale.proc_locale(ctx)

			# setup system names (environments / hosts / hostname / machine-info)
			names.proc_names(ctx)

			# running add files hooks (for initramfs settings)
			filesystem.add_files_all(ctx, "pre-initramfs")

			# running scripts hooks (for initramfs settings)
			script.run_scripts(ctx, "pre-initramfs")

			# recreate initramfs
			mkinitcpio.proc_mkinitcpio(ctx)

			# reset machine-id (never duplicated machine id)
			systemd.proc_machine_id(ctx)
		finally:
			# kill spawned daemons (gpg-agent, dirmngr, ...)
			ctx.cgroup.kill_all()

			# remove mount points
			mount.undo_mounts(ctx)

		# cleanup unneeded files
		cleanup(ctx)

	# running add files hooks (after build rootfs)
	filesystem.add_files_all(ctx, "post-build")

	# running scripts hooks (after build rootfs)
	script.run_scripts(ctx, "post-build")

	# reload user database before create images
	ctx.reload_passwd()

	# create images and initialize bootloader
	try:
		# create disk and filesystem image
		image.proc_image(ctx)

		# generate fstab
		fstab.proc_fstab(ctx)

		# install grub bootloader
		grub.proc_grub(ctx)

		# install systemd-boot bootloader
		systemd_boot.proc_systemd_boot(ctx)

		# install extlinux bootloader
		extlinux.proc_extlinux(ctx)

		# running add files hooks (for bootloader settings)
		filesystem.add_files_all(ctx, "post-fs")

		# running scripts hooks (after bootloader rootfs)
		script.run_scripts(ctx, "post-fs")

		# copy rootfs into image
		do_copy(ctx, ctx.get_rootfs(), ctx.get_mount())
	finally:
		ctx.cleanup()

	# finish
	log.info("build done!")
	os.sync()
	log.info(f"your images are in {ctx.get_output()}")
	ctx.run_external(["ls", "-lh", ctx.get_output()])
