import os
from logging import getLogger
from builder.lib.blkid import Blkid
from builder.disk.layout.gpt.types import DiskTypesGPT
from builder.disk.content import ImageContentBuilder
from builder.lib.config import ArchBuilderConfigError
from builder.lib.mount import MountPoint
from builder.lib.utils import path_to_name
log = getLogger(__name__)


remove_rootflags = [
	"defaults", "auto", "noauto", "user", "nouser", "ownner", "comment",
	"nosuid", "nodev", "noexec", "noatime", "nodiratime", "relatime",
	"bind", "rw", "ro", "remount", "bind", "fail", "nofail",
]

class FileSystemBuilder(ImageContentBuilder):
	blkid: Blkid = Blkid()
	fstype_map: dict = {
		"fat12": "vfat",
		"fat16": "vfat",
		"fat32": "vfat",
	}

	def __init__(self, builder: ImageContentBuilder):
		self._creator = None
		super().__init__(builder)

	def proc_cmdline_root(self, cfg: dict, mnt: MountPoint):
		ccfg = self.builder.ctx.config_orig
		mnt.remove_option("ro")
		mnt.remove_option("rw")
		for opt in mnt.option:
			if opt.startswith("x-"):
				mnt.option.remove(opt)
		if "kernel" not in ccfg: ccfg["kernel"] = {}
		kern = ccfg["kernel"]
		if "cmdline" not in kern: kern["cmdline"] = []
		cmds: list[str] = kern["cmdline"]
		if any(cmdline.startswith("root=") for cmdline in cmds):
			raise ArchBuilderConfigError("root already set in cmdline")
		if mnt.target != "/":
			log.warning(f"root target is not / ({mnt.target})")
		if not mnt.source.startswith("/") and "=" not in mnt.source:
			log.warning(f"bad root source ({mnt.source})")
		ecmds = [
			"ro", "rootwait=10",
			f"root={mnt.source}",
		]
		if mnt.fstype != "none":
			ecmds.append(f"rootfstype={mnt.fstype}")
		if len(mnt.option) > 0:
			copied = mnt.clone()
			for opt in copied.option:
				if opt in remove_rootflags:
					copied.option.remove(opt)
			ecmds.append(f"rootflags={copied.options}")
		scmds = " ".join(ecmds)
		log.debug(f"add root cmdline {scmds}")
		cmds.extend(ecmds)
		self.builder.ctx.resolve_subscript()

	def resolve_dev_tag(self, dev: str, mnt: MountPoint):
		dev = dev.upper()
		match dev:
			case "UUID" | "LABEL":
				log.warning(f"'{dev}=' maybe unsupported by kernel")
				if dev in self.properties: val = self.properties[dev]
				else: val = self.blkid.get_tag_value(None, dev, self.builder.device)
			case "PARTUUID" | "PARTLABEL":
				val = self.properties[dev] if dev in self.properties else None
			case _: raise ArchBuilderConfigError(f"unsupported device type {dev}")
		if not val: raise ArchBuilderConfigError(f"property {dev} not found")
		mnt.source = f"{dev}={val}"

	def proc_grow(self, cfg: dict, mnt: MountPoint):
		root = self.builder.ctx.get_rootfs()
		if "ptype" not in cfg:
			log.warning("no partition type set, grow filesystem only")
			mnt.option.append("x-systemd.growfs")
			return
		ptype = DiskTypesGPT.lookup_one_uuid(cfg["ptype"])
		if ptype is None: raise ArchBuilderConfigError(f"unknown type {cfg['ptype']}")
		mnt.option.append("x-systemd.growfs")
		conf = "grow-%s.conf" % path_to_name(mnt.target)
		repart = os.path.join(root, "etc/repart.d", conf)
		os.makedirs(os.path.dirname(repart), mode=0o0755, exist_ok=True)
		fsname, fsuuid = None, None
		dev = self.builder.device
		if "fsname" in cfg: fsname = cfg["fsname"]
		if "fsuuid" in cfg: fsuuid = cfg["fsuuid"]
		if fsname is None: fsname = self.blkid.get_tag_value(None, "LABEL", dev)
		if fsuuid is None: fsuuid = self.blkid.get_tag_value(None, "UUID", dev)
		with open(repart, "w") as f:
			f.write("[Partition]\n")
			f.write(f"Type={ptype}\n")
			f.write(f"Format={mnt.fstype}\n")
			if fsname: f.write(f"Label={fsname}\n")
			if fsuuid: f.write(f"UUID={fsuuid}\n")
		log.info(f"generated repart config {repart}")

	def proc_fstab(self, cfg: dict):
		mnt = MountPoint()
		ccfg = self.builder.ctx.config
		use_fstab = True
		fstab = cfg["fstab"] if "fstab" in cfg else {}
		rfstab = ccfg["fstab"] if "fstab" in ccfg else {}
		mnt.target = cfg["mount"]
		mnt.fstype = cfg["fstype"]
		dev = None
		if "dev" in fstab: dev = fstab["dev"]
		if "dev" in rfstab: dev = rfstab["dev"]
		if dev: self.resolve_dev_tag(dev, mnt)
		if mnt.target == "/": mnt.fs_passno = 1
		elif not mnt.virtual: mnt.fs_passno = 2
		if "target" in fstab: mnt.target = fstab["target"]
		if "source" in fstab: mnt.source = fstab["source"]
		if "type" in fstab: mnt.fstype = fstab["type"]
		if "dump" in fstab: mnt.fs_freq = fstab["dump"]
		if "passno" in fstab: mnt.fs_passno = fstab["passno"]
		if "flags" in fstab:
			flags = fstab["flags"]
			if type(flags) is str: mnt.options = flags
			elif type(flags) is list: mnt.option = flags
			else: raise ArchBuilderConfigError("bad flags")
		if mnt.source is None:
			if not fstab:
				log.info(f"skip fstab item {mnt}")
				mnt.source = self.builder.device
				use_fstab = False
			else:
				raise ArchBuilderConfigError("incomplete fstab")
		if len(self.builder.ctx.fstab.find_target(mnt.target)) > 0:
			raise ArchBuilderConfigError(f"duplicate fstab target {mnt.target}")
		if mnt.fstype in self.fstype_map:
			mnt.fstype = self.fstype_map[mnt.fstype]
		if use_fstab and cfg.get("grow", False):
			self.proc_grow(cfg, mnt)
		mnt.fixup()
		if use_fstab:
			log.debug(f"add fstab entry {mnt}")
			self.builder.ctx.fstab.append(mnt)
		self.builder.ctx.mtab.append(mnt)
		self.builder.ctx.fsmap[mnt.source] = self.builder.device
		if use_fstab and "boot" in fstab and fstab["boot"]:
			self.proc_cmdline_root(cfg, mnt.clone())

	@property
	def fstype(self) -> str:
		if "fstype" not in self.builder.config:
			raise ArchBuilderConfigError("fstype not set")
		return self.builder.config["fstype"]

	@property
	def creator(self):
		if not self._creator:
			from builder.disk.filesystem.creator import FileSystemCreators
			FileSystemCreators.init()
			self._creator = FileSystemCreators.find_builder(self.fstype)
			if self._creator is None: raise ArchBuilderConfigError(f"unsupported fs type {self.fstype}")
		return self._creator(self.fstype, self, self.builder.config)

	def format(self):
		self.creator.create()

	def copy(self):
		self.creator.copy()

	def auto_create_image(self) -> bool:
		return self.creator.auto_create_image()

	def build(self):
		self.format()
		if "mount" in self.builder.config:
			self.proc_fstab(self.builder.config)

	def build_post(self):
		self.copy()
