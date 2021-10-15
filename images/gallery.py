#!/usr/bin/env python3
#
# Copyright (C) 2020-2021 Dirk Bergstrom <dirk@otisbean.com>. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#

import json
import re
import datetime
from io import BytesIO
from pathlib import Path
import argparse
import sys
import logging
import binascii

from PIL import Image, ImageFilter, ExifTags
from titlecase import titlecase


CONTENT_FILE = "content.json"
MAX_WIDTH = 1600
MAX_HEIGHT = 1400
THUMBNAIL_HEIGHT = 225

logging.basicConfig(level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s")


def isoformat(timeint):
    return datetime.datetime.fromtimestamp(timeint) \
        .replace(microsecond=0).isoformat().replace("T", " ")


def exif_to_isodate(exif_date):
    dt = datetime.datetime.strptime(exif_date, "%Y:%m:%d %H:%M:%S")
    return dt.replace(microsecond=0).isoformat().replace("T", " ")


def fixexif(val):
    """Remove non-ASCII characters and strip leading & trailing
    whitespace.

    The text fields in EXIF data are full of garbage.
    """
    return "".join([x for x in val if (x.isprintable() or x.isspace())]).strip()


def read_exif_metadata(img, data):
    """Read EXIF from the photo and map it to Nanogallery data.

    Model, Make & LensModel => exifModel
    Flash => exifFlash  (as "" or "Flash")
    FocalLength => exifFocalLength  (as an integer)
    FNumber => exifFStop  (as '.1f')
    ExposureTime => exifExposure  (as either int seconds or a fraction)
    ISOSpeedRatings => ExifIso
    DateTimeOriginal => exifTime
    UserComment => description  ("Caption" field in DigiKam)
    DocumentName => title  ("Name" field in DigiKam)
    """
    # EXIF tag data is a disgusting swamp of badly formatted information
    raw_exif = img._getexif()
    if not raw_exif:
        logging.info("No exif in photo")
        return
    exif_tags = {ExifTags.TAGS.get(k, k): v for k, v in raw_exif.items()}

    mod = exif_tags.get('Model')
    if mod:
        mod = fixexif(mod)
        make = exif_tags.get('Make')
        if make:
            make = titlecase(fixexif(make))
            mod = "{} {}".format(make, mod)
        lens = exif_tags.get('LensModel')
        if lens:
            mod = "{}; {}".format(mod, fixexif(lens))
        data['exifModel'] = mod

    # exif flash is a bitmask where the first bit is "did it fire"
    flash = exif_tags.get('Flash', 0)
    data['exifFlash'] = "Flash" if (flash & 1) else ""

    fl = exif_tags.get('FocalLength')
    if fl:
        if isinstance(fl, tuple):
            # A tuple.  One hopes that the first element is always the focal
            # length, and the second 1, but...
            data['exifFocalLength'] = int(fl[0] / fl[1])
        else:
            # Hope it's a number...
            data['exifFocalLength'] = int(fl)

    fn = exif_tags.get('FNumber')
    if fn:
        if isinstance(fn, tuple):
            # Another tuple
            data['exifFStop'] = "{:.1f}".format(fn[0] / fn[1])
        else:
            data['exifFStop'] = "{:.1f}".format(float(fn))

    et = exif_tags.get('ExposureTime')
    if et:
        if isinstance(et, tuple):
            # Tuple.  Format sub-second times as a fraction.
            if et[1] == 1:
                et = '{}"'.format(et[0])
            else:
                et = "{}/{}".format(*et)
        data['exifExposure'] = float(et)

    iso = exif_tags.get('ISOSpeedRatings')
    if iso:
        data['exifIso'] = iso

    dto = exif_tags.get('DateTimeOriginal')
    if dto:
        data['exifTime'] = exif_to_isodate(dto)

    # UserComment => description
    # Along the way we convert newlines to <br> tags and linkify URLs
    uc = exif_tags.get('UserComment')
    if uc:
        uc = fixexif(uc.decode())
        if uc.startswith("ASCII"):
            # As written by DigiKam the UserComment field has
            # a prefix of 'ASCII\x00\x00\x00'
            uc = uc[5:]
        uc = re.sub("\n", "\n<br>", uc)
        uc = re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', uc)
        data['description'] = uc

    dn = exif_tags.get('DocumentName')
    if (dn and not
            re.search(r'\.jpg', dn, re.IGNORECASE) and not
            re.search(r'\d{5}', dn)):
        # Looks like an actual title, not just a filename
        data['title'] = dn


def doit(directory, force, force_resize, dry_run):
    """Do the thing.
    """
    orig_dir = directory / "originals"
    if not orig_dir.exists():
        print("Expected to find a sub-directory named 'originals' "
            "containing image files.", file=sys.stderr)
        sys.exit(1)
    content_file = directory / CONTENT_FILE
    if not content_file.exists():
        content = []
    else:
        with content_file.open() as cf:
            content = json.load(cf)
    old = {c['filename']: c for c in content}
    new = []
    done = []
    for path in sorted(orig_dir.glob('*.jpg')):
        if path.is_file():
            # A candidate
            mtime = isoformat(path.stat().st_mtime)
            oe = old.get(path.name)
            if (force or oe is None or oe.get('mtime') != mtime):
                # File is new or changed
                new.append(dict(
                    filename=path.name,
                    mtime=mtime,
                    path=path,
                    ID=re.sub(r'[^\w-]', '-', path.stem),
                    downloadURL=f"/images/{orig_dir.name}/{path.name}"))
            else:
                # We have up-to-date info for this file
                done.append(oe)

    if len(new) == 0:
        logging.info("No changes, exiting.")
        return

    resized_dir = directory / "resized"
    if not resized_dir.exists():
        resized_dir.mkdir()

    # Process new files
    for data in new:
        path = data.pop("path")
        logging.info("Processing %s", path.name)
        img = Image.open(path)

        # Save the EXIF data so we can write it back out
        exif_bytes = img.info.get('exif', b'')

        if img.width > MAX_WIDTH or img.height > MAX_HEIGHT:
            # Image too large, need maxpect image for web display
            logging.info("Image too large (%d x %d)", img.width, img.height)
            resized_name = f"web-{path.name}"
            resized_path = resized_dir / resized_name
            if resized_path.exists() and not force_resize:
                logging.info("Reading size of existing maxpect")
                maxpect = Image.open(resized_path)
            else:
                logging.info("Making maxpect")
                maxpect = img.copy()
                # thumbnail() method modifies image, preserves aspect ratio.
                # Image.LANCZOS is the best quality and seems plenty fast
                # Image.BICUBIC is faster but lower quality.
                maxpect.thumbnail(
                    (MAX_WIDTH, MAX_HEIGHT), resample=Image.LANCZOS)
                logging.debug('Saving maxpect as "%s"', resized_path)
                if not dry_run:
                    maxpect.save(resized_path,
                        quality=90,
                        progressive=True,
                        optimized=True,
                        exif=exif_bytes,
                        icc_profile=img.info.get('icc_profile'))
            data["imgWidth"] = maxpect.width
            data["imgHeight"] = maxpect.height
            data["src"] = f'/images/{resized_dir.name}/{resized_name}'
        else:
            data["src"] = data["downloadURL"]
            data["imgWidth"] = img.width
            data["imgHeight"] = img.height

        read_exif_metadata(img, data)
        if "title" not in data:
            # Nothing in EXIF, use the filename
            if not re.search(r'\d{5}', path.name):
                # Doesn't look like a serial number, assume it's text and try
                # to make it pretty.
                data['title'] = titlecase(re.sub(r'[_-]', ' ', path.stem))
            else:
                data['title'] = path.name

        # make thumbnail (cropping to 90%)
        thumb_path = resized_dir / f"thumb-{path.name}"
        logging.info("Making thumbnail %s", thumb_path)
        crop_coords = (
            img.width / 20,
            img.height / 20,
            img.width - img.width / 20,
            img.height - img.height / 20
        )
        thumb = img.crop(crop_coords)
        hratio = thumb.height / THUMBNAIL_HEIGHT
        thumb.thumbnail((thumb.width * hratio, THUMBNAIL_HEIGHT))
        if not dry_run:
            thumb.save(thumb_path)
        data["srct"] = f"/images/{resized_dir.name}/{thumb_path.name}"
        data["imgtWidth"] = thumb.width
        data["imgtHeight"] = thumb.height

        # Get dominant colors
        #  Resize to ~20x20, blur, create gif, base64 encode
        # (Fancier method: https://github.com/fengsp/color-thief-py)
        logging.info("Creating 'dominant colors' gif")
        thumb.thumbnail((15, 15))
        blurred = thumb.filter(filter=ImageFilter.BLUR)
        bio = BytesIO()
        blurred.save(bio, format="GIF")
        gif_encoded = binascii.b2a_base64(bio.getvalue()).decode('utf8')
        # Add to new dict
        data['imageDominantColors'] = f"data:image/gif;base64,{gif_encoded}"

        done.append(data)

    # FIXME Remove orphaned thumbs and originals

    # Write new CONTENT_FILE
    done.sort(key=lambda x: x.get('exifTime', x['mtime']), reverse=True)
    if dry_run:
        print(json.dumps(done, indent=1), file=sys.stderr)
    else:
        # Make symlink to latest thumbnail image
        latest = Path(done[0]['srct']).name
        symlink_path = resized_dir / 'latest.jpg'
        try:
            if symlink_path.exists() or symlink_path.is_symlink():
                logging.debug("unlinking old symlink %s", symlink_path)
                symlink_path.unlink()
            logging.info("Creating 'latest.jpg' symlink %s -> %s", symlink_path, latest)
            (symlink_path).symlink_to(latest)
        except OSError as e:
            logging.error("Failed to create 'latest.jpg' symlink: " + str(e))
        # Write JSON
        logging.info("Writing %s", directory / CONTENT_FILE)
        with (directory / CONTENT_FILE).open(mode='w') as fp:
            json.dump(done, fp, indent=1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", metavar="DIR", type=str,
        help="Directory holding images and content.json")
    parser.add_argument("--dry-run", action="store_true",
        help="Don't modify any files")
    parser.add_argument("--force", action="store_true",
        help="Reprocess all files")
    parser.add_argument("--force-resize", action="store_true",
        help="Reprocess all files and recreate maxpect images")
    args = parser.parse_args()
    doit(Path(args.directory),
         (args.force or args.force_resize), args.force_resize, args.dry_run)
