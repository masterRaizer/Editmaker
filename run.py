#!/usr/bin/env python3
"""EditMaker from source.

  python run.py                 UI in the browser: http://127.0.0.1:8765
  python run.py --desktop       UI in a native window (pywebview)
  python run.py check           ffmpeg / GPU check
  python run.py selftest DIR    synthetic clip + beat -> 4 variants with watermark into DIR
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--desktop" in sys.argv:
        from src.desktop import main
        raise SystemExit(main([a for a in sys.argv[1:] if a != "--desktop"]))
    from src.cli import main
    raise SystemExit(main())
