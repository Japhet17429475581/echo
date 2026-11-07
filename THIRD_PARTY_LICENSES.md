# Third-party components

Echo's MIT license does not replace dependency licenses. The runtime retains wheel `.dist-info` license files, Debian `/usr/share/doc/*/copyright` and `/usr/share/common-licenses`; do not strip them from redistributed images. Exact Python versions and hashes are in `uv.lock`, `engines/*/requirements.lock` and `tools/requirements.lock`. System versions below were read from the built Bookworm image; APT can update them on rebuild.

| Component | License | Source |
|---|---|---|
| Sherlock Project 0.16.2 | MIT; full text in `licenses/sherlock.txt` | https://github.com/sherlock-project/sherlock |
| Maigret 0.6.6 | MIT; full text in `licenses/maigret.txt` | https://github.com/soxoj/maigret |
| ExifTool 12.57 (`12.57+dfsg-1`) | Artistic or GPL-1.0-or-later; BZZ.pm GPL-1.0-or-later, see Debian copyright | https://exiftool.org/ ; https://sources.debian.org/src/libimage-exiftool-perl/ |
| Tesseract 5.3.0 (`5.3.0-2`) | Apache-2.0 | https://github.com/tesseract-ocr/tesseract |
| Tesseract eng/rus tessdata_fast (`1:4.1.0-2`) | Apache-2.0 | https://github.com/tesseract-ocr/tessdata_fast |
| WhatWeb 0.5.5 (`0.5.5-1`) | GPL-2.0-or-later, unmodified separate CLI | https://github.com/urbanadventurer/WhatWeb ; https://sources.debian.org/src/whatweb/ |
| zxing-cpp 3.1.1 | Apache-2.0 | https://github.com/zxing-cpp/zxing-cpp |
| Trafilatura 2.3.1 | Apache-2.0 | https://github.com/adbar/trafilatura |
| dnstwist 20250130 | Apache-2.0 | https://github.com/elceef/dnstwist |
| dnspython 2.9.2 | ISC | https://github.com/rthalley/dnspython |
| Pillow 13.0.0 | MIT-CMU (PIL/Pillow license; bundled codecs have additional notices) | https://github.com/python-pillow/Pillow |
| libmagic / file (`1:5.44-3`) | BSD-style and individual file notices | https://www.darwinsys.com/file/ |
| libseccomp (`2.5.4-1+deb12u1`) | LGPL-2.1-or-later | https://github.com/seccomp/libseccomp |
| aiogram | MIT | https://github.com/aiogram/aiogram |
| SQLAlchemy / Alembic | MIT | https://www.sqlalchemy.org/ |
| asyncpg | Apache-2.0 | https://github.com/MagicStack/asyncpg |
| redis-py | MIT | https://github.com/redis/redis-py |
| HTTPX | BSD-3-Clause | https://github.com/encode/httpx |
| aiohttp | Apache-2.0 and MIT | https://github.com/aio-libs/aiohttp |
| Pydantic / pydantic-settings | MIT | https://github.com/pydantic |
| Jinja2 / MarkupSafe | BSD-3-Clause | https://palletsprojects.com/ |
| ReportLab | BSD | https://www.reportlab.com/ |
| tldextract | BSD-3-Clause | https://github.com/john-kurkowski/tldextract |
| Public Suffix List | MPL-2.0; included unmodified via tldextract snapshot | https://publicsuffix.org/list/ |
| Beautiful Soup | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| psutil | BSD-3-Clause | https://github.com/giampaolo/psutil |
| age | BSD-3-Clause | https://github.com/FiloSottile/age |
| DejaVu fonts | Bitstream Vera / public domain contributions | https://dejavu-fonts.github.io/ |
| tini | MIT | https://github.com/krallin/tini |
| PostgreSQL server | PostgreSQL License | https://www.postgresql.org/about/licence/ |
| Redis server | image-version-specific licensing; check selected official image (Redis 8 offers AGPLv3/RSALv2/SSPLv1 options) | https://redis.io/legal/licenses/ |

The engine dependency trees additionally include their own HTTP, parsing, numerical, data and terminal libraries. Their installed licenses and complete lock files are part of the distribution; upstream data remains part of the unmodified installed engine package. No upstream branding implies endorsement.

For source availability obligations, obtain exact upstream versions from the linked repositories/PyPI and the selected official Debian/Redis image sources. For the bundled PSL snapshot, the corresponding source is available at https://publicsuffix.org/list/public_suffix_list.dat and inside the tldextract distribution. Echo does not modify that data.

ExifTool and WhatWeb remain separate unmodified executables; Echo communicates through bounded JSON/files. When redistributing their binaries, preserve the complete notices/licenses and provide corresponding source for the exact Debian package version under its applicable terms. Debian source packages include packaging patches; an upstream tag alone may not match the shipped binary. Retrieve them from sources.debian.org or the matching Debian archive and include them with your distribution as required. Do not describe the complete container as MIT-only. Linux Landlock is a kernel ABI, not a vendored userspace library; the deployment's kernel distribution retains its own license obligations.
