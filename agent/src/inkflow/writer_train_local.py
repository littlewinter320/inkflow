"""Opt-in local LoRA runner for reviewed Writer samples.

Requires a trainable local model and separately installed Torch, Transformers
and PEFT. Never downloads a model or publishes an adapter. A finished adapter
is a candidate, not a change to the configured DeepSeek Writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from .utils import atomic_write_json


def _on_d_drive(path: Path) -> bool:
    return path.resolve().drive.upper() == "D:"


def _load_batches(materials: Path, reviewed: Path) -> tuple[list[list[dict[str, Any]]], str]:
    manifest = json.loads((materials / "manifest.json").read_text(encoding="utf-8"))
    batches = [json.loads(line) for line in (materials / "batches.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if manifest.get("state") != "structurally_prepared_not_training_ready" or not 20 <= len(batches) <= 30:
        raise ValueError("素材清单不完整，须先生成20～30个候选批次")
    holdout = {row["book_id"] for row in manifest["holdout"]}
    all_rows = []
    fingerprint = hashlib.sha256()
    for batch in batches:
        path = reviewed / f"batch-{batch['batch_no']:04d}.jsonl"
        raw = path.read_bytes()
        fingerprint.update(path.name.encode("ascii") + b"\0" + raw)
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if not rows:
            raise ValueError(f"{path.name} 没有已核对的样本")
        allowed_units = set(batch["unit_ids"])
        for row in rows:
            if row.get("book_id") != batch["book_id"] or row["book_id"] in holdout:
                raise ValueError(f"{path.name} 的作品来源不匹配或属于留出作品")
            ids = row.get("source_unit_ids")
            if not isinstance(ids, list) or not ids or not set(ids) <= allowed_units:
                raise ValueError(f"{path.name} 的素材定位不属于该批次")
            if row.get("quality_accepted") is not True or row.get("training_rights_confirmed") is not True:
                raise ValueError(f"{path.name} 含未核对质量或未确认用途的样本")
            for key in ("task", "context", "completion", "reviewed_by"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError(f"{path.name} 缺少 {key}")
        all_rows.append(rows)
    return all_rows, fingerprint.hexdigest()


def train(
    materials: Path, reviewed: Path, model_path: Path, output: Path,
    *, start_adapter: Path | None = None, max_length: int = 8192, learning_rate: float = 1e-4,
) -> dict[str, Any]:
    for name, path in (("素材", materials), ("已审样本", reviewed), ("基础模型", model_path), ("输出", output)):
        if not _on_d_drive(path):
            raise ValueError(f"{name}必须位于 D 盘：{path}")
    if start_adapter is not None and not _on_d_drive(start_adapter):
        raise ValueError("接续适配器必须位于 D 盘")
    if not model_path.is_dir() or not (model_path / "config.json").is_file():
        raise ValueError("基础模型目录不完整；本工具不会自行下载模型")
    if not 1024 <= max_length <= 32768 or learning_rate <= 0:
        raise ValueError("上下文长度或学习率无效")
    batches, data_hash = _load_batches(materials, reviewed)
    output.mkdir(parents=True, exist_ok=True)
    progress_path = output / "progress.json"
    identity = {
        "schema": "inkflow-writer-lora-v1", "materials": str(materials.resolve()),
        "reviewed_sha256": data_hash, "base_model": str(model_path.resolve()),
        "starting_adapter": str(start_adapter.resolve()) if start_adapter else None,
        "max_length": max_length, "learning_rate": learning_rate,
        "integration_status": "candidate_not_connected_to_writer",
    }
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if any(progress.get(key) != value for key, value in identity.items()):
            raise ValueError("已有训练状态的素材、模型或参数不同；请新建输出目录")
        # A crash after renaming a complete checkpoint but before updating
        # progress must not retrain that batch or overwrite its adapter.
        while progress["last_completed_batch"] < len(batches):
            next_no = progress["last_completed_batch"] + 1
            complete = output / f"checkpoint-{next_no:04d}"
            if not (complete / "result.json").is_file() or not (complete / "optimizer.pt").is_file():
                break
            result = json.loads((complete / "result.json").read_text(encoding="utf-8"))
            if result.get("batch") != next_no or result.get("samples") != len(batches[next_no - 1]):
                raise ValueError(f"第{next_no}批检查点与已审核样本不匹配")
            progress.update({"last_completed_batch": next_no, "last_checkpoint": str(complete), "samples_seen": progress["samples_seen"] + result["samples"], "elapsed_seconds": float(progress.get("elapsed_seconds", 0.0)) + float(result.get("elapsed_seconds", 0.0))})
            atomic_write_json(progress_path, progress)
    else:
        if any(output.iterdir()):
            raise ValueError("输出目录非空且没有可续跑状态，请换一个目录")
        progress = {**identity, "last_completed_batch": 0, "last_checkpoint": None, "samples_seen": 0, "elapsed_seconds": 0.0}
        atomic_write_json(progress_path, progress)
    if progress["last_completed_batch"] == len(batches):
        return progress

    # All material/rights checks finish before importing large training libraries.
    try:
        import torch
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("缺少本地训练依赖；本工具不会自动安装或下载") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("未检测到 CUDA 显卡；停止以免意外使用 CPU 长时间训练")
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    if not getattr(tokenizer, "chat_template", None):
        raise ValueError("基础模型缺少聊天模板，无法安全构造 Writer 训练输入")
    base = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, trust_remote_code=False, torch_dtype=dtype)
    base.config.use_cache = False
    base.to("cuda")
    previous = progress["last_checkpoint"] or (str(start_adapter) if start_adapter else None)
    if previous:
        model = PeftModel.from_pretrained(base, previous, is_trainable=True)
    else:
        model = get_peft_model(base, LoraConfig(task_type="CAUSAL_LM", r=8, lora_alpha=16, lora_dropout=0.05, target_modules="all-linear"))
    model.train()
    optimizer = torch.optim.AdamW((parameter for parameter in model.parameters() if parameter.requires_grad), lr=learning_rate)
    if progress["last_checkpoint"]:
        state = torch.load(Path(progress["last_checkpoint"]) / "optimizer.pt", map_location="cpu", weights_only=True)
        optimizer.load_state_dict(state)
    started = time.monotonic()
    elapsed_before = float(progress.get("elapsed_seconds", 0.0))
    for index in range(progress["last_completed_batch"], len(batches)):
        batch_started = time.monotonic()
        rows = batches[index][:]
        random.Random(20260927 + index).shuffle(rows)
        losses = []
        for row in rows:
            if time.monotonic() - started >= 12 * 60 * 60:
                raise RuntimeError("本次训练运行已到12小时上限；下次从最近完整检查点续跑")
            user_text = f"写作要求：{row['task'].strip()}\n必要前文：{row['context'].strip()}"
            prompt_ids = tokenizer.apply_chat_template([{"role": "user", "content": user_text}], tokenize=True, add_generation_prompt=True)
            answer_ids = tokenizer.encode(row["completion"].strip(), add_special_tokens=False)
            if tokenizer.eos_token_id is not None:
                answer_ids.append(tokenizer.eos_token_id)
            if not answer_ids or len(prompt_ids) + len(answer_ids) > max_length:
                raise ValueError(f"第{index + 1}批样本超出 {max_length} token；请按完整场景人工拆短后再训练")
            input_ids = torch.tensor([prompt_ids + answer_ids], device="cuda")
            labels = torch.tensor([[-100] * len(prompt_ids) + answer_ids], device="cuda")
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast("cuda", dtype=dtype, enabled=dtype == torch.bfloat16):
                loss = model(input_ids=input_ids, labels=labels).loss
            if not torch.isfinite(loss):
                raise RuntimeError(f"第{index + 1}批损失非有限值，保留上一检查点")
            loss.backward()
            torch.nn.utils.clip_grad_norm_((parameter for parameter in model.parameters() if parameter.requires_grad), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        checkpoint = output / f"checkpoint-{index + 1:04d}"
        temporary = output / f".saving-{uuid.uuid4().hex}"
        temporary.mkdir()
        model.save_pretrained(temporary)
        tokenizer.save_pretrained(temporary)
        torch.save(optimizer.state_dict(), temporary / "optimizer.pt")
        atomic_write_json(temporary / "result.json", {"batch": index + 1, "samples": len(rows), "mean_loss": sum(losses) / len(losses), "elapsed_seconds": time.monotonic() - batch_started, "quality_status": "not_evaluated"})
        temporary.replace(checkpoint)
        progress.update({"last_completed_batch": index + 1, "last_checkpoint": str(checkpoint), "samples_seen": progress["samples_seen"] + len(rows), "elapsed_seconds": elapsed_before + time.monotonic() - started})
        atomic_write_json(progress_path, progress)
        print(json.dumps({"completed_batch": index + 1, "of": len(batches), "checkpoint": str(checkpoint)}, ensure_ascii=False), flush=True)
    return progress


def main() -> None:
    parser = argparse.ArgumentParser(description="使用已审核样本训练本地 Writer 适配器；不下载模型")
    parser.add_argument("materials", type=Path)
    parser.add_argument("reviewed", type=Path)
    parser.add_argument("local_model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start-adapter", type=Path)
    parser.add_argument("--max-length", type=int, default=8192)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    args = parser.parse_args()
    result = train(args.materials, args.reviewed, args.local_model, args.output, start_adapter=args.start_adapter, max_length=args.max_length, learning_rate=args.learning_rate)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
