#!/usr/bin/env python3
"""
Реальная LLM-нагрузка на инференс-сервер (prefill + decode) для тепловых тестов.

Даёт именно тот тепловой профиль, который карта видит в проде: prefill ~190 Вт
пиками +decode ~140 Вт непрерывно. Параллельно пишет telemetry рядом с
gpu_fan_sweep.py --monitor-only.

Пример:
  python3 llm_load.py --url http://localhost:8087 --seconds 300 --prompt-tokens 4000
"""

import argparse
import json
import statistics
import time
import urllib.request


def post(url, payload, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    return body, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8087")
    ap.add_argument("--model", default="qwen3.8-flash-next")
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--prompt-tokens", type=int, default=4000)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    filler = ("Расскажи подробно про архитектуру графических ускорителей. " * 900)
    prompt = filler[:args.prompt_tokens * 4]

    pp, tg, wall = [], [], []
    t_end = time.time() + args.seconds
    n = 0
    while time.time() < t_end:
        try:
            body, dt = post(f"{args.url}/v1/chat/completions", {
                "model": args.model,
                "messages": [{"role": "user", "content": prompt +
                                          "\nДай краткий вывод."}],
                "max_tokens": args.max_tokens,
                "temperature": 0.0,
                "stream": False,
            })
        except Exception as e:
            print(f"[load] ошибка запроса: {e}", flush=True)
            time.sleep(3)
            continue

        n += 1
        u = body.get("usage") or {}
        t = body.get("timings") or {}
        pe = u.get("prompt_eval_count") or t.get("prompt_eval_count") or 0
        pc = u.get("completion_tokens") or t.get("predicted_tokens") or 0
        pms = t.get("prompt_ms") or t.get("prompt_eval_ms") or 0
        cms = t.get("predicted_ms") or t.get("predicted_ms") or t.get("completion_ms") or 0
        if pe and pms:
            pp.append(pe / (pms / 1000.0))
        if pc and cms:
            tg.append(pc / (cms / 1000.0))
        wall.append(dt)
        print(f"[load] req#{n}: pp={pe} in {pms:.0f}ms, tg={pc} in {cms:.0f}ms, wall={dt:.1f}s",
              flush=True)
        time.sleep(args.sleep)

    def stat(x):
        return f"{statistics.mean(x):.1f} (min {min(x):.1f} / max {max(x):.1f})" if x else "н/д"

    print("\n=== ИТОГ LLM-нагрузки ===")
    print(f"запросов: {n}")
    print(f"prefill, t/s: {stat(pp)}")
    print(f"decode,  t/s: {stat(tg)}")
    print(f"wall,    s:   {stat(wall)}")


if __name__ == "__main__":
    main()
