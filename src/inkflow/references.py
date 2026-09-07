from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .errors import ProjectError
from .project import InkFlowProject
from .utils import atomic_write_json, atomic_write_text, content_hash, read_text_fallback, safe_filename, utc_now


class ReferenceService:
    def __init__(self, project: InkFlowProject):
        self.project = project
        self.raw_dir = project.internal / "references" / "raw"
        self.feature_dir = project.internal / "references" / "features"

    def import_text(self, source_path: str | Path) -> dict:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise ProjectError(f"参考文件不存在：{source}")
        text = read_text_fallback(source)
        reference_id = content_hash(text)[:16]
        destination = self.raw_dir / f"{reference_id}-{safe_filename(source.stem)}.txt"
        atomic_write_text(destination, text)
        self._update_manifest(reference_id, "local_file", str(source), destination)
        return {"reference_id": reference_id, "path": str(destination), "characters": len(text)}

    async def fetch_url(
        self,
        url: str,
        *,
        source_type: str = "public_url",
        source_metadata: dict | None = None,
    ) -> dict:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProjectError("只支持 http/https 公开网页。")
        async with httpx.AsyncClient(follow_redirects=True, timeout=45, headers={"User-Agent": "InkFlow/0.3"}) as client:
            response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise ProjectError(f"当前 MVP 不处理该内容类型：{content_type}")
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup(["script", "style", "noscript", "svg"]):
            node.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else parsed.netloc
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        if len(text) < 200:
            raise ProjectError("网页正文过短，可能是动态空壳、登录页或错误页。")
        reference_id = content_hash(response.url.__str__() + text)[:16]
        destination = self.raw_dir / f"{reference_id}-{safe_filename(title)}.txt"
        atomic_write_text(destination, text)
        self._update_manifest(
            reference_id,
            source_type,
            str(response.url),
            destination,
            source_metadata=source_metadata,
        )
        return {
            "reference_id": reference_id,
            "title": title,
            "url": str(response.url),
            "path": str(destination),
            "characters": len(text),
        }

    async def search_public(self, query: str, *, limit: int = 6) -> dict:
        """Search public web pages without importing or persisting any result.

        Search is deliberately separated from ``fetch_url``.  A user can read
        the title, snippet and source first, then explicitly choose which page
        belongs in the project reference library.
        """

        normalized_query = re.sub(r"\s+", " ", query).strip()
        if not normalized_query:
            raise ProjectError("请输入要查找的写作问题或资料主题。")
        if len(normalized_query) > 200:
            raise ProjectError("搜索问题请控制在 200 个字符以内；可以拆成两次更具体的搜索。")
        result_limit = max(1, min(int(limit), 10))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers=headers) as client:
                response = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": normalized_query, "kl": "cn-zh"},
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProjectError(
                "公开搜索暂时不可用。你仍可粘贴已知网页链接或导入本地资料；稍后也可以更换搜索适配器。"
            ) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict[str, str]] = []
        for node in soup.select(".result"):
            anchor = node.select_one("a.result__a")
            if anchor is None:
                continue
            title = anchor.get_text(" ", strip=True)
            url = self._search_result_url(str(anchor.get("href") or ""))
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            parsed = urlparse(url)
            if not title or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "source": parsed.netloc,
                    "snippet": snippet,
                }
            )
            if len(results) >= result_limit:
                break
        if not results:
            raise ProjectError(
                "公开搜索页没有返回可识别结果，可能是暂时限制或页面结构发生变化。"
                "请换一个更具体的问题，或直接粘贴已知网页链接。"
            )
        return {
            "query": normalized_query,
            "adapter": "duckduckgo_html_v1",
            "results": results,
            "notice": "搜索结果尚未进入资料库；只有点击“导入这页”的来源才会保存到当前小说项目。",
        }

    @staticmethod
    def _search_result_url(raw_url: str) -> str:
        if not raw_url:
            return ""
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        parsed = urlparse(raw_url)
        redirect_target = parse_qs(parsed.query).get("uddg")
        if redirect_target:
            return unquote(redirect_target[0])
        return raw_url

    async def fetch_fanqie_public(self, url: str) -> dict:
        """Capture a public Fanqie page without bypassing logins or access controls.

        This deliberately is a small adapter rather than a crawler framework:
        it keeps the main writing engine dependency-free and records enough
        metadata to replace only this adapter if the site markup changes.
        """

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        supported = host == "fanqienovel.com" or host.endswith(".fanqienovel.com")
        if parsed.scheme not in {"http", "https"} or not supported:
            raise ProjectError("番茄适配器只接受 fanqienovel.com 的公开 http/https 链接。")
        metadata = {
            "adapter": "fanqie_public_v1",
            "capture_mode": "public_html",
            "structure_change_action": "页面正文不足时提示用户导入合法文本或启用可选浏览器后端；不绕过访问控制。",
        }
        try:
            result = await self.fetch_url(
                url,
                source_type="fanqie_public",
                source_metadata=metadata,
            )
        except ProjectError as exc:
            if "网页正文过短" not in str(exc):
                raise
            raise ProjectError(
                "番茄公开页没有返回可抽取的正文，可能是动态页面、登录页或页面结构已变化。"
                "墨流未尝试绕过访问控制；请导入你有权使用的 TXT/MD，或后续启用可选浏览器后端。"
            ) from exc
        return result | metadata

    def analyze(self, reference_id: str) -> dict:
        matches = list(self.raw_dir.glob(f"{reference_id}-*.txt"))
        if not matches:
            raise ProjectError(f"找不到参考资料：{reference_id}")
        text = matches[0].read_text(encoding="utf-8")
        paragraphs = [item.strip() for item in re.split(r"\n+", text) if item.strip()]
        sentences = [item for item in re.split(r"[。！？!?]+", text) if item.strip()]
        chapter_marks = re.findall(r"(?m)^\s*第[零一二三四五六七八九十百千万\d]+[章节回卷].{0,40}$", text)
        dialogue_chars = sum(len(item) for item in re.findall(r"[“\"]([^”\"]+)[”\"]", text))
        endings = [item[-100:] for item in paragraphs if len(item) >= 100][-20:]
        feature = {
            "reference_id": reference_id,
            "source_file": matches[0].name,
            "analyzed_at": utc_now(),
            "characters": len(text),
            "paragraph_count": len(paragraphs),
            "sentence_count": len(sentences),
            "detected_chapters": len(chapter_marks),
            "average_paragraph_chars": round(len(text) / max(1, len(paragraphs)), 2),
            "average_sentence_chars": round(len(text) / max(1, len(sentences)), 2),
            "dialogue_ratio": round(dialogue_chars / max(1, len(text)), 4),
            "sample_chapter_endings": endings,
            "note": "MVP 确定性特征；后续可增加分层模型分析，不直接把全文送入写作上下文。",
        }
        path = self.feature_dir / f"{reference_id}.json"
        atomic_write_json(path, feature)
        return feature

    def list_references(self) -> list[dict]:
        manifest_path = self.project.internal / "references" / "manifest.json"
        if not manifest_path.is_file():
            return []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        result: list[dict] = []
        for item in list(manifest.get("items") or []):
            if not isinstance(item, dict) or not item.get("reference_id"):
                continue
            feature_path = self.feature_dir / f"{item['reference_id']}.json"
            result.append(
                {
                    **item,
                    "analyzed": feature_path.is_file(),
                    "feature_path": (
                        str(feature_path.relative_to(self.project.root)) if feature_path.is_file() else None
                    ),
                }
            )
        return sorted(result, key=lambda value: str(value.get("imported_at") or ""), reverse=True)

    def _update_manifest(
        self,
        reference_id: str,
        source_type: str,
        source: str,
        destination: Path,
        *,
        source_metadata: dict | None = None,
    ) -> None:
        manifest_path = self.project.internal / "references" / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {"items": []}
        else:
            manifest = {"items": []}
        manifest.setdefault("items", [])
        manifest["items"] = [item for item in manifest["items"] if item.get("reference_id") != reference_id]
        item = {
            "reference_id": reference_id,
            "source_type": source_type,
            "source": source,
            "stored_path": str(destination.relative_to(self.project.root)),
            "imported_at": utc_now(),
        }
        if source_metadata:
            item["adapter_metadata"] = source_metadata
        manifest["items"].append(item)
        atomic_write_json(manifest_path, manifest)
