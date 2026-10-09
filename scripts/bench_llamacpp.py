# -*- coding: utf-8 -*-
"""P2.1 链路实测（llama.cpp 形态）：加载本地 GGUF，生成报告解读，统计速度与质量。"""
import time
from llama_cpp import Llama

MODEL = r"D:\360MoveData\Users\29903\Desktop\工作资料\AI训练报告引擎\eng\models\qwen2.5-1.5b-instruct-q4_k_m.gguf"

SYSTEM = (
    "你是射箭教练的数据解读助手。规则引擎给出结构化结论，你只做解读，"
    "不得改变任何数值、不得新增结论。输出不超过 120 字。"
)
SKELETON = (
    "运动员张明，本周（2026-W32）平均环 9.8，较基线 9.4 进步 +0.4 环，"
    "最小变化量 MDC=0.3，属于真实进步；撒放用时均值 0.42 秒，"
    "散布 68 毫米较基线 75 毫米收窄 7 毫米。样本：3 场 90 箭，达标。"
)


def main():
    t_load = time.time()
    llm = Llama(model_path=MODEL, n_ctx=2048, n_threads=8, verbose=False)
    print(f"模型加载: {time.time()-t_load:.1f}s")

    t0 = time.time()
    out = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": SKELETON}],
        temperature=0.2, max_tokens=512,
    )
    secs = time.time() - t0
    usage = out.get("usage", {})
    total = usage.get("completion_tokens", 0)
    print(f"总耗时: {secs:.1f}s | 生成 {total} token | 吞吐: {total/secs:.1f} token/s")
    print("--- 输出 ---")
    print(out["choices"][0]["message"]["content"].strip())


if __name__ == "__main__":
    main()
