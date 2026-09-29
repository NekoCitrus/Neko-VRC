"""Generate Python waveform tables from the official dglab-kit TypeScript files."""

from __future__ import annotations

import argparse
import pprint
import re
from pathlib import Path


ENTRY_RE = re.compile(
    r"\[[^]]+\]:\s*\{\s*label:\s*\{\s*cn:\s*'([^']+)',\s*en:\s*'([^']+)',"
    r"\s*\},\s*raw:\s*\[(.*?)\],\s*\},",
    re.DOTALL,
)
FRAME_RE = re.compile(r"'([0-9A-F]{16})'")


def parse(path: Path) -> dict[str, tuple[str, ...]]:
    source = path.read_text(encoding="utf-8")
    result = {}
    for chinese_name, _english_name, raw in ENTRY_RE.findall(source):
        result[chinese_name] = tuple(FRAME_RE.findall(raw))
    if not result:
        raise RuntimeError(f"No waveforms found in {path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    root = Path(args.source)
    coyote = parse(root / "src" / "waveform" / "coyote.ts")
    opossum = parse(root / "src" / "waveform" / "ovc.ts")
    if (len(coyote), len(opossum)) != (24, 20):
        raise RuntimeError(f"Unexpected waveform counts: {len(coyote)}, {len(opossum)}")
    content = (
        '"""Official DG-LAB Socket waveform tables.\n\n'
        'Generated from dungeonlab-open/dglab-kit (GPL-3.0).\n'
        f'Source commit: {args.commit}\n'
        '"""\n\n'
        f"COYOTE_WAVEFORMS = {pprint.pformat(coyote, width=100, sort_dicts=False)}\n\n"
        f"OPOSSUM_WAVEFORMS = {pprint.pformat(opossum, width=100, sort_dicts=False)}\n\n"
        "WAVEFORMS_BY_DEVICE = {\n"
        "    'coyote': COYOTE_WAVEFORMS,\n"
        "    'opossum': OPOSSUM_WAVEFORMS,\n"
        "}\n"
        "WAVEFORM_NAMES_BY_DEVICE = {\n"
        "    device_kind: tuple(waveforms)\n"
        "    for device_kind, waveforms in WAVEFORMS_BY_DEVICE.items()\n"
        "}\n"
    )
    Path(args.output).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
