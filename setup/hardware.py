"""Detect GPUs, driver, interconnect, CPU, RAM and disks.

NVML (nvidia-ml-py) first, nvidia-smi as a fallback. Set PERSEUS_FAKE_HW to a
JSON file to run every check on a machine without GPUs (tests, dry runs).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIB = 1024 ** 3


class HardwareError(Exception):
    pass


@dataclass
class GPU:
    index: int
    name: str
    vram_bytes: int
    compute_cap: tuple[int, int]

    @property
    def vram_gib(self) -> float:
        return self.vram_bytes / GIB


@dataclass
class Disk:
    mount: str
    free_bytes: int
    nvme: bool | None  # None = couldn't tell (overlay roots in containers)


@dataclass
class Hardware:
    gpus: list[GPU]
    driver_version: str
    cuda_version: str            # highest CUDA the driver supports
    interconnect: str            # single | nvlink | partial-nvlink | pcie | unknown
    cpu_cores: int
    ram_bytes: int
    disks: list[Disk] = field(default_factory=list)

    @property
    def min_vram_bytes(self) -> int:
        return min(g.vram_bytes for g in self.gpus)

    @property
    def min_compute_cap(self) -> tuple[int, int]:
        return min(g.compute_cap for g in self.gpus)

    def summary(self) -> str:
        if not self.gpus:
            return "no NVIDIA GPU"
        names = {g.name for g in self.gpus}
        name = names.pop() if len(names) == 1 else "mixed GPUs"
        return f"{len(self.gpus)} × {name} ({self.gpus[0].vram_gib:.0f} GiB)"

    def to_dict(self) -> dict:
        return asdict(self)


def detect(repo_root: Path | None = None) -> Hardware:
    fake = os.environ.get("PERSEUS_FAKE_HW")
    if fake:
        return _from_fake(Path(fake), repo_root)
    gpus, driver, cuda = _gpus_nvml()
    if gpus is None:
        gpus, driver, cuda = _gpus_smi()
    return Hardware(
        gpus=gpus,
        driver_version=driver,
        cuda_version=cuda,
        interconnect=_interconnect(len(gpus)),
        cpu_cores=os.cpu_count() or 1,
        ram_bytes=_ram_bytes(),
        disks=candidate_disks(repo_root),
    )


def _from_fake(path: Path, repo_root: Path | None) -> Hardware:
    d = json.loads(path.read_text())
    gpus = [GPU(i, g["name"], int(g["vram_gib"] * GIB), tuple(g["compute_cap"]))
            for i, g in enumerate(d["gpus"])]
    disks = [Disk(x["mount"], int(x["free_gib"] * GIB), x.get("nvme")) for x in d.get("disks", [])]
    return Hardware(gpus, d.get("driver_version", "580.65.06"), d.get("cuda_version", "13.0"),
                    d.get("interconnect", "single" if len(gpus) == 1 else "nvlink"),
                    d.get("cpu_cores", 32), int(d.get("ram_gib", 256) * GIB),
                    disks or candidate_disks(repo_root))


def _gpus_nvml():
    try:
        import pynvml  # provided by nvidia-ml-py
    except ImportError:
        return None, "", ""
    try:
        pynvml.nvmlInit()
    except Exception:
        return None, "", ""
    try:
        driver = _s(pynvml.nvmlSystemGetDriverVersion())
        cuda_int = pynvml.nvmlSystemGetCudaDriverVersion()  # e.g. 13000
        cuda = f"{cuda_int // 1000}.{(cuda_int % 1000) // 10}"
        gpus = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            gpus.append(GPU(i, _s(pynvml.nvmlDeviceGetName(h)),
                            int(pynvml.nvmlDeviceGetMemoryInfo(h).total),
                            tuple(pynvml.nvmlDeviceGetCudaComputeCapability(h))))
        return gpus, driver, cuda
    finally:
        pynvml.nvmlShutdown()


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _gpus_smi():
    if not shutil.which("nvidia-smi"):
        raise HardwareError("nvidia-smi not found: no NVIDIA driver on this box. Use a GPU image with "
                            "the driver installed, or run preflight.sh --install-driver on a VM")
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30, check=True).stdout
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError) as e:
        raise HardwareError(f"nvidia-smi failed ({e}); is the driver loaded?")
    return parse_smi_query(out) + (_cuda_from_header(head),)


def parse_smi_query(out: str) -> tuple[list[GPU], str]:
    gpus, driver = [], ""
    for line in out.strip().splitlines():
        idx, name, mem_mib, driver, cc = [p.strip() for p in line.split(",")]
        major, minor = cc.split(".")
        gpus.append(GPU(int(idx), name, int(float(mem_mib)) * 1024 * 1024, (int(major), int(minor))))
    return gpus, driver


def _cuda_from_header(text: str) -> str:
    m = re.search(r"CUDA Version:\s*([\d.]+)", text)
    return m.group(1) if m else ""


def _interconnect(n: int) -> str:
    if n <= 1:
        return "single"
    try:
        out = subprocess.run(["nvidia-smi", "topo", "-m"], capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return parse_topology(out, n)


def parse_topology(out: str, n: int) -> str:
    """Read the GPU×GPU block of `nvidia-smi topo -m`: NV# cells mean NVLink."""
    nv = total = 0
    for line in out.splitlines():
        cells = line.split()
        # data rows start at column 0 with "GPUn"; the header row is indented
        if not cells or line[:1].isspace() or not re.fullmatch(r"GPU\d+", cells[0]):
            continue
        row = int(cells[0][3:])
        for col, cell in enumerate(cells[1:1 + n]):
            if col != row:
                total += 1
                nv += bool(re.fullmatch(r"NV\d+", cell))
    if total == 0:
        return "unknown"
    return "nvlink" if nv == total else ("partial-nvlink" if nv else "pcie")


def _ram_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "mqueue", "securityfs",
           "debugfs", "tracefs", "pstore", "bpf", "fusectl", "configfs", "hugetlbfs", "autofs",
           "squashfs", "nsfs", "binfmt_misc", "efivarfs", "ramfs", "rpc_pipefs", "nfsd"}


def candidate_disks(repo_root: Path | None = None) -> list[Disk]:
    """Writable local mounts with their free space, NVMe ones flagged."""
    seen: dict[str, Disk] = {}
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        mounts = []
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mount, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype in SKIP_FS or mount.startswith(("/proc", "/sys", "/dev", "/run", "/boot", "/snap")):
            continue
        if mount in seen or not os.access(mount, os.W_OK):
            continue
        try:
            free = shutil.disk_usage(mount).free
        except OSError:
            continue
        seen[mount] = Disk(mount, free, _is_nvme(dev))
    if repo_root is not None:
        root = str(repo_root)
        if not any(root == m or root.startswith(m.rstrip("/") + "/") for m in seen):
            seen[root] = Disk(root, shutil.disk_usage(root).free, None)
    return list(seen.values())


def _is_nvme(dev: str) -> bool | None:
    if "nvme" in dev:
        return True
    name = Path(dev).name
    if not dev.startswith("/dev/") or not name:
        return None
    base = re.sub(r"\d+$", "", name)
    rot = Path(f"/sys/block/{base}/queue/rotational")
    try:
        return False if rot.read_text().strip() == "1" else None
    except OSError:
        return None


def pick_data_dir(disks: list[Disk], need_bytes: int, repo_root: Path) -> tuple[Path, Disk]:
    """The data directory on the fastest disk with room: known NVMe first, then most free space.

    Never inside the checkout: on the checkout's own disk it sits beside it (../perseus-data).
    """
    fits = [d for d in disks if d.free_bytes >= need_bytes]
    if not fits:
        best = max(disks, key=lambda d: d.free_bytes, default=None)
        have = f"{best.free_bytes / GIB:.0f} GiB free on {best.mount}" if best else "no writable disk found"
        raise HardwareError(f"not enough disk: the model needs {need_bytes / GIB:.0f} GiB with headroom, "
                            f"{have}. Attach a larger local NVMe volume")
    best = sorted(fits, key=lambda d: (d.nvme is not True, -d.free_bytes))[0]
    root = str(repo_root)
    on_repo_disk = best.mount == root or root.startswith(best.mount.rstrip("/") + "/")
    if on_repo_disk and os.access(repo_root.parent, os.W_OK):
        return repo_root.parent / "perseus-data", best
    if best.mount == "/" or on_repo_disk:
        return Path.home() / "perseus-data", best
    return Path(best.mount) / "perseus", best


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v))


def driver_ok(found: str, minimum: str) -> bool:
    return version_tuple(found) >= version_tuple(minimum)
