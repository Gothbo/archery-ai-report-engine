# -*- coding: utf-8 -*-
"""P2.1 链路实测：调用本地 Ollama 生成一段报告解读，统计速度与质量。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:11434"


def generate(model: str, prompt: str) -> tuple[dict, float]:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.2, "num_ctx": 2048}}).encode()
    req = urllib.request.Request(BASE + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out, time.time() - t0


def main():
    # 模拟规则引擎喂给 LLM 的结论骨架（与接入方案 §4.1 同构）
    prompt = (
        "你是射箭教练的数据解读助手。以下是规则引擎给出的结构化结论，"
        "请用不超过 120 字的中文解读，不得改变任何数值：\n"
    )
    skeleton = (
        "运动员张明，本周（2026-W32）平均环 9.8，较基线 9.4 进步 +0.4 环，"
        "最小变化量 MDC=0.3，属于真实进步；撒放用时均值 0.42 秒，"
        "散布 68 毫米较基线 75 毫米收窄 7 毫米。样本：3 场 90 箭，达标。"
    )
    res, secs = generate("deepseek-r1-distill-qwen:1.5b", prompt + skeleton)
    total = res.get("eval_count", 0)
    eval_ms = res.get("eval_count", 0) / (res.get("eval_duration", 1) / 1e9) if res.get("eval_duration") else 0
    print(f"模型: {res.get('model')}")
    print(f"总耗时: {secs:.1f}s | 生成 {total} token | 吞吐: {total/secs:.1f} token/s")
    if res.get("eval_duration"):
        print(f"纯生成速率: {eval_ms:.1f} token/s (不含首token/排队)")
    print("--- 输出 ---")
    print(res.get("response", ""))


if __name__ == "__main__":
    main()
