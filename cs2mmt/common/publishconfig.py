"""Reading and editing ``Properties/PublishConfiguration.xml`` without reformatting it.

ModPublisher reads this file for every upload and also writes some of it back to the site
(NewVersion and Update both rewrite the page text), so edits must be surgical: comments, line
breaks and the hand-written LongDescription stay exactly as they are. ElementTree is used only
to read; every change is a targeted text replacement.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .versioning import is_numeric_version

__all__ = [
    "PublishConfigError",
    "PublishInfo",
    "read_info",
    "get_value",
    "set_value",
    "get_text",
    "set_text",
    "validate",
    "SHORT_DESCRIPTION_MAX",
]

SHORT_DESCRIPTION_MAX = 200  # ModPublisher: "shortDescription: Must be a string between 1 and 200 length"


class PublishConfigError(ValueError):
    pass


@dataclass
class PublishInfo:
    mod_id: str = ""
    display_name: str = ""
    short_description: str = ""
    long_description: str = ""
    thumbnail: str = ""
    mod_version: str = ""
    change_log: str = ""
    game_version: str = ""
    access_level: str = ""
    forum_link: str = ""
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    external_links: List[str] = field(default_factory=list)


def _attr_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _text_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _element_value(root: ET.Element, tag: str) -> str:
    el = root.find(tag)
    if el is None:
        return ""
    if "Value" in el.attrib:
        return el.attrib["Value"]
    return (el.text or "").strip("\r\n")


def read_info(text: str) -> PublishInfo:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise PublishConfigError("PublishConfiguration.xml is not valid XML: %s" % e) from e
    if root.tag != "Publish":
        raise PublishConfigError("root element is <%s>, expected <Publish>" % root.tag)
    info = PublishInfo(
        mod_id=_element_value(root, "ModId"),
        display_name=_element_value(root, "DisplayName"),
        short_description=_element_value(root, "ShortDescription"),
        long_description=_element_value(root, "LongDescription"),
        thumbnail=_element_value(root, "Thumbnail"),
        mod_version=_element_value(root, "ModVersion"),
        change_log=_element_value(root, "ChangeLog"),
        game_version=_element_value(root, "GameVersion"),
        access_level=_element_value(root, "AccessLevel"),
        forum_link=_element_value(root, "ForumLink"),
    )
    info.dependencies = [d.attrib.get("Id", "") for d in root.findall("Dependency") if d.attrib.get("Id")]
    info.tags = [t.attrib.get("Value", "") for t in root.findall("Tag") if t.attrib.get("Value")]
    info.external_links = [x.attrib.get("Url", "") for x in root.findall("ExternalLink") if x.attrib.get("Url")]
    return info


def _self_closing(tag: str) -> "re.Pattern[str]":
    return re.compile(r'<%s(\s[^>]*?)?\sValue="([^"]*)"([^>]*?)/>' % re.escape(tag))


def get_value(text: str, tag: str) -> Optional[str]:
    m = _self_closing(tag).search(text)
    return None if m is None else m.group(2)


def set_value(text: str, tag: str, value: str, insert_after: Optional[str] = None) -> str:
    """Sets ``<Tag Value="..." />``. Adds the element after ``insert_after`` when it is missing."""
    pattern = _self_closing(tag)
    m = pattern.search(text)
    if m is not None:
        start, end = m.span(2)
        return text[:start] + _attr_escape(value) + text[end:]
    if insert_after is None:
        raise PublishConfigError("<%s> not found" % tag)
    return _insert_after(text, insert_after, '<%s Value="%s" />' % (tag, _attr_escape(value)))


def _paired(tag: str) -> "re.Pattern[str]":
    return re.compile(r"(<%s(?:\s[^>]*)?>)(.*?)(</%s\s*>)" % (re.escape(tag), re.escape(tag)), re.S)


def get_text(text: str, tag: str) -> Optional[str]:
    m = _paired(tag).search(text)
    if m is None:
        v = get_value(text, tag)
        return v
    inner = m.group(2)
    return inner.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def set_text(text: str, tag: str, content: str, insert_after: Optional[str] = None) -> str:
    """Sets the text content of ``<Tag>...</Tag>``.

    A self-closing ``<Tag />`` or ``<Tag Value="" />`` is turned into a paired element; a missing
    element is added after ``insert_after``.
    """
    escaped = _text_escape(content)
    m = _paired(tag).search(text)
    if m is not None:
        return text[: m.start(2)] + escaped + text[m.end(2):]
    sc = re.compile(r"<%s(\s[^>]*?)?/>" % re.escape(tag)).search(text)
    if sc is not None:
        return text[: sc.start()] + "<%s>%s</%s>" % (tag, escaped, tag) + text[sc.end():]
    if insert_after is None:
        raise PublishConfigError("<%s> not found" % tag)
    return _insert_after(text, insert_after, "<%s>%s</%s>" % (tag, escaped, tag))


def _insert_after(text: str, anchor_tag: str, element: str) -> str:
    """Inserts ``element`` on its own line after the element ``anchor_tag``, same indentation."""
    m = _paired(anchor_tag).search(text) or re.compile(r"<%s(\s[^>]*?)?/>" % re.escape(anchor_tag)).search(text)
    if m is None:
        raise PublishConfigError("anchor <%s> not found" % anchor_tag)
    line_start = text.rfind("\n", 0, m.start()) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text[: m.end()] + newline + indent + element + text[m.end():]


def validate(info: PublishInfo, mode: str, root_dir: Optional[Path] = None) -> List[str]:
    """Problems that would make ModPublisher reject the upload. ``mode``: NewVersion/Update/Publish."""
    problems: List[str] = []
    if not info.display_name.strip():
        problems.append("DisplayName is empty")
    n = len(info.short_description)
    if n < 1 or n > SHORT_DESCRIPTION_MAX:
        problems.append("ShortDescription has %d characters; ModPublisher needs 1-%d" % (n, SHORT_DESCRIPTION_MAX))
    if not info.mod_version.strip():
        problems.append("ModVersion is empty")
    elif not is_numeric_version(info.mod_version):
        problems.append("ModVersion %r is not numeric (the game parses the UI module version as a number)" % info.mod_version)
    if mode in ("NewVersion", "Update"):
        if not info.mod_id.strip():
            problems.append("ModId is empty: the first upload (Publish) has to be done by hand")
        if not info.change_log.strip():
            problems.append("ChangeLog is empty (NewVersion and Update need one)")
    if mode == "Publish" and info.mod_id.strip():
        problems.append("ModId is already set; a first Publish needs an empty ModId")
    if root_dir is not None and info.thumbnail:
        if not (root_dir / info.thumbnail).is_file():
            problems.append("Thumbnail %s does not exist" % info.thumbnail)
    return problems
