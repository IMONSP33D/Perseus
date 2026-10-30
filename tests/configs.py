"""Config folders for tests, made from Config/templates the way configuration.sh makes them."""

import tempfile
from pathlib import Path

from perseus import CONFIG_TEMPLATES
from perseus.config import CONFIG_FILES

TOKEN = "hf_" + "a" * 34


def template_texts(model: str = "Qwen/Qwen3-32B", token: str = TOKEN) -> dict[str, str]:
    texts = {n: (CONFIG_TEMPLATES / n).read_text() for n in CONFIG_FILES}
    texts["provider.toml"] = (texts["provider.toml"].replace('"org/model-name"', f'"{model}"')
                              .replace('"hf_..."', f'"{token}"'))
    return texts


def edited(texts: dict[str, str], *pairs: tuple[str, str]) -> dict[str, str]:
    """Each (old, new) applied to the one file that contains `old`."""
    out = dict(texts)
    for old, new in pairs:
        hits = [n for n, t in out.items() if old in t]
        assert len(hits) == 1, f"{old!r} found in {hits or 'no file'}"
        out[hits[0]] = out[hits[0]].replace(old, new)
    return out


def write_dir(texts: dict[str, str], parent: Path | None = None) -> Path:
    folder = (parent or Path(tempfile.mkdtemp())) / "Config"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (folder / name).write_text(text)
    return folder


def config_dir(*pairs: tuple[str, str], parent: Path | None = None, **kw) -> Path:
    return write_dir(edited(template_texts(**kw), *pairs), parent)
