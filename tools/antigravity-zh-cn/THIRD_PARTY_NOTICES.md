# Third-party source notices

The following files are copied without modification from
[wadewu-ml/antigravity-i18n](https://github.com/wadewu-ml/antigravity-i18n)
at commit `229658784a100e024325b6bb2ae49f574eae86d1`:

| Local file | Upstream file |
| --- | --- |
| `source/zh-CN.json` | `assets/locales/zh-CN.json` |
| `source/engine.jsfrag` | `assets/patches/engine.jsfrag` |
| `source/menu.jsfrag` | `assets/patches/menu.jsfrag` |
| `source/LICENSE` | `LICENSE` |

These files are licensed under the MIT License, copyright © 2026
antigravity-i18n Contributors. The complete license is preserved in
[`source/LICENSE`](source/LICENSE).

`prepare.py` builds a detached candidate from an official application supplied
locally by the operator. It adds a local-host restriction and exception boundary
around the UI translation code, additional macOS menu labels, and a reverse
lookup that preserves update-menu action dispatch after label translation.
It does not install, sign, launch, or change the supplied application.

No official Antigravity executable, application archive, or extracted official
JavaScript is distributed in this directory. Generated candidates and backups
belong in the ignored project `runtime/` directory. The resource-only backup
created by the build tool is not a complete macOS application backup.

Antigravity is a Google product. This localization code is unofficial and is
not affiliated with or endorsed by Google. Local macOS installation and signing
require separate review: local signing does not retain Google's signing trust
and may require per-application library-validation changes.
