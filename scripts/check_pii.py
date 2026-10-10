# -*- coding: utf-8 -*-
"""发布前脱敏闸门：在公开仓库（git tracked）文件中查找真实运动员姓名与可还原身份号。

命中即退出码 1，供发布前 checklist 调用：
    python scripts/check_pii.py
白名单为已知合成值（测试用），不视为真实 PII。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 独立 18 位身份号（前后非数字，末位可为 X）
ID_PAT = re.compile(r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])")
# 已知真实姓名（如新增真实姓名，追加到此处）
REAL_NAMES = ["葛靖"]
# 已知合成身份号（测试 fixture），放行
SYNTHETIC_IDS = {"110101200001011234", "11010119900101123X"}
# 已知真实运动员的派生 ID（"1"+真实身份证号，可逆）：硬编码即视为 PII 残留
REAL_DERIVED_IDS = [
    "1422802198808030396", "1141031200001240050", "1141123200608110039",
    "1210281199405013635", "1140106199607063629", "1340123200901291832",
    "1231084198505082732", "1411330198206154565", "1341021198005285058",
    "1140429198908295614", "1020946108869583360", "1055300236639907354",
    "1130521200901300540", "1130503200904110619", "1025459157238184794",
    "1141002200611300047", "1069416507186232282", "1140421200405063618",
    "1340122200412281514", "1110112201709140017", "1130503200902250642",
]


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "-C", ROOT, "ls-files", "-z"], text=True)
    return [f for f in out.split("\0") if f]


def main() -> int:
    hits: list[str] = []
    for rel in _tracked_files():
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    for m in ID_PAT.finditer(line):
                        if m.group().upper() in SYNTHETIC_IDS:
                            continue
                        hits.append(f"ID   {rel}:{i}: {m.group()}")
                    for name in REAL_NAMES:
                        if name in line:
                            hits.append(f"NAME {rel}:{i}: {name}")
                    for did in REAL_DERIVED_IDS:
                        if did in line:
                            hits.append(f"DERIVED {rel}:{i}: {did}")
        except OSError:
            pass

    for h in hits:
        print(h)
    if hits:
        print(f"---- FAIL: {len(hits)} 处疑似 PII 残留 ----")
        return 1
    print("---- OK: 未发现真实姓名/可还原身份号 ----")
    return 0


if __name__ == "__main__":
    sys.exit(main())