"""Common patterns for contact identifiers and media paths in free text."""
import re

CONTACT = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15}|(?:\+?86[- ]?)?1[3-9](?:[- ]?\d){9})(?!\d)")
MEDIA_PATH = re.compile(r"[^\s，。；、\"'<>]+\.(?:mp4|mov|avi|mkv|webm|m4v)(?:\?[^\s，。；、\"'<>]*)?", re.I)
