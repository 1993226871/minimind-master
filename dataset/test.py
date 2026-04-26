import re
import json
import argparse
from pathlib import Path
from typing import List, Dict, Optional

import fitz  # pymupdf
from tqdm import tqdm


CN_NUM = "一二三四五六七八九十百千万零〇0-9"


def normalize_text(s: str) -> str:
    """基础清洗。"""
    s = s.replace("\u3000", " ")
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n\s+", "\n", s)
    return s.strip()


def extract_lines_from_pdf(pdf_path: str, start_page: int = 1, end_page: Optional[int] = None) -> List[str]:
    """
    从 PDF 中抽取文本行。
    start_page / end_page 使用 1-based 页码。
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    if end_page is None:
        end_page = total_pages

    all_lines = []

    for page_idx in range(start_page - 1, min(end_page, total_pages)):
        page = doc[page_idx]
        text = page.get_text("text")
        text = normalize_text(text)

        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # 跳过目录页、封面页
        if is_toc_or_cover_page(lines):
            continue

        # 去掉页眉页脚
        lines = remove_page_noise(lines)

        all_lines.extend(lines)

    return all_lines


def is_toc_or_cover_page(lines: List[str]) -> bool:
    """
    判断是否为封面/目录页。
    你的 PDF 前面有校训、目录页，所以这里做过滤。
    """
    joined = "\n".join(lines)

    if not joined:
        return True

    # 封面或校训页
    if "校训" in joined or "明德厚学" in joined or "求是创新" in joined:
        return True

    # 目录页特征
    if "目 录" in joined or "目录" in joined:
        return True

    # 很多目录行会有 .......... 页码
    dot_lines = sum(1 for line in lines if re.search(r"\.{3,}\s*\d+\s*$", line))
    if dot_lines >= 3:
        return True

    # 罗马数字页码
    if len(lines) <= 3 and any(line in {"I", "II", "III", "IV", "V"} for line in lines):
        return True

    return False


def remove_page_noise(lines: List[str]) -> List[str]:
    """
    去掉页码、孤立罗马数字等。
    注意：不直接删除文件标题，因为标题需要用于切分。
    """
    cleaned = []

    for line in lines:
        line = line.strip()

        # 纯页码
        if re.fullmatch(r"\d+", line):
            continue

        # 罗马数字页码
        if re.fullmatch(r"[IVX]+", line):
            continue

        # 常见空白噪声
        if line in {"-", "—"}:
            continue

        cleaned.append(line)

    return cleaned


def is_doc_title(line: str) -> bool:
    """
    判断是否为一份规章制度文件标题。
    适配你的手册中类似：
    华中科技大学博士研究生培养工作规定
    华中科技大学研究生学籍管理实施细则
    华中科技大学学生违纪处分规定
    关于开展博士学位论文重点审核工作的通知
    """
    line = line.strip()

    if len(line) < 6 or len(line) > 60:
        return False

    if "第" in line and "章" in line:
        return False

    if re.search(r"校研〔|研字〔|经 .* 审议|二○|202[0-9] 年", line):
        return False

    title_suffix = (
        "规定",
        "办法",
        "细则",
        "通知",
        "守则",
        "管理办法",
        "实施办法",
        "实施细则",
        "处理办法",
        "撰写规定",
        "评审规定",
        "处理规定",
    )

    if line.startswith("华中科技大学") and line.endswith(title_suffix):
        return True

    if line.startswith("关于") and line.endswith(("通知", "规定", "办法")):
        return True

    return False


def is_chapter_title(line: str) -> bool:
    """
    判断章节标题，如：第一章 总则
    """
    return bool(re.fullmatch(rf"第[{CN_NUM}]+章\s*.+", line.strip()))


def is_article_start(line: str) -> bool:
    """
    判断条款开头，如：第一条 ...
    """
    return bool(re.match(rf"^第[{CN_NUM}]+条", line.strip()))


def merge_broken_lines(lines: List[str]) -> List[str]:
    """
    PDF 抽取后经常把一个句子拆成多行。
    这里做轻量合并：
    - 文档标题、章节标题、条款开头保留为新行
    - 普通正文尽量拼接到上一行
    """
    merged = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if is_doc_title(line) or is_chapter_title(line) or is_article_start(line):
            merged.append(line)
        else:
            if not merged:
                merged.append(line)
            else:
                prev = merged[-1]

                # 如果上一行是标题，不合并
                if is_doc_title(prev) or is_chapter_title(prev):
                    merged.append(line)
                else:
                    # 中文正文断行直接拼接
                    merged[-1] = prev + line

    return merged


def parse_articles(lines: List[str]) -> List[Dict]:
    """
    将整本手册解析为 article 级别记录。
    每条记录包含：
    - doc_title
    - chapter_title
    - article_no
    - article_text
    """
    lines = merge_broken_lines(lines)

    articles = []
    current_doc = None
    current_chapter = None
    current_article_no = None
    current_article_lines = []

    def flush_article():
        nonlocal current_article_no, current_article_lines

        if current_doc and current_article_no and current_article_lines:
            article_text = "".join(current_article_lines).strip()
            article_text = clean_article_text(article_text)

            if len(article_text) >= 20:
                articles.append({
                    "doc_title": current_doc,
                    "chapter_title": current_chapter or "",
                    "article_no": current_article_no,
                    "article_text": article_text,
                })

        current_article_no = None
        current_article_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 新文件标题
        if is_doc_title(line):
            # 如果是页眉重复标题，且与当前标题一致，则跳过
            if current_doc == line:
                continue

            flush_article()
            current_doc = line
            current_chapter = ""
            continue

        # 章节标题
        if is_chapter_title(line):
            flush_article()
            current_chapter = line
            continue

        # 条款
        if is_article_start(line):
            flush_article()

            m = re.match(rf"^(第[{CN_NUM}]+条)", line)
            current_article_no = m.group(1) if m else ""

            current_article_lines = [line]
            continue

        # 普通正文：只有进入某个条款后才记录
        if current_article_no:
            current_article_lines.append(line)

    flush_article()
    return articles


def clean_article_text(text: str) -> str:
    """
    清理条款文本。
    """
    text = normalize_text(text)
    text = text.replace("\n", "")
    text = re.sub(r"\s+", " ", text)

    # 去掉多余空格
    text = text.replace(" ，", "，")
    text = text.replace(" 。", "。")
    text = text.replace(" ；", "；")
    text = text.replace(" ：", "：")

    return text.strip()


def extract_topic(article_text: str) -> str:
    """
    从条款中粗略抽取主题，生成更自然的问题。
    不依赖大模型，只做规则处理。
    """
    text = re.sub(rf"^第[{CN_NUM}]+条", "", article_text).strip()

    # 优先截取第一个逗号/句号/分号之前的内容
    seg = re.split(r"[，。；：]", text)[0].strip()

    # 去掉过泛的开头
    seg = re.sub(r"^(研究生|博士生|硕士生|学生|新生|学校|培养单位|院（系）|导师)", "", seg).strip()
    seg = re.sub(r"^(应当|应|须|必须|可以|可|不得|一般|在校期间)", "", seg).strip()

    if not seg:
        seg = text[:24]

    # 限长，避免问题过长
    if len(seg) > 24:
        seg = seg[:24]

    return seg


def build_answer(article: Dict) -> str:
    doc_title = article["doc_title"]
    chapter_title = article["chapter_title"]
    article_no = article["article_no"]
    article_text = article["article_text"]

    source = f"根据《{doc_title}》"
    if chapter_title:
        source += f"{chapter_title}"
    source += f"{article_no}"

    # 回答中保留原始条款，减少幻觉
    answer = (
        f"{source}，{article_text} "
        f"具体办理和执行口径应以学校最新研究生手册、院系通知及相关部门解释为准。"
    )
    return answer


def build_sft_samples(articles: List[Dict], qa_per_article: int = 3) -> List[Dict]:
    """
    将条款转换成 MiniMind SFT conversations 格式。
    """
    samples = []

    for article in articles:
        doc_title = article["doc_title"]
        chapter_title = article["chapter_title"]
        article_no = article["article_no"]
        topic = extract_topic(article["article_text"])
        answer = build_answer(article)

        questions = [
            f"《{doc_title}》{article_no}主要规定了什么？",
            f"研究生手册中关于{topic}有什么规定？",
            f"请解释一下{doc_title}中{article_no}的内容。",
            f"如果遇到{topic}相关情况，应该按照研究生手册怎么处理？",
            f"{chapter_title}{article_no}讲的是什么？" if chapter_title else f"{article_no}讲的是什么？",
        ]

        # 去重并限制数量
        unique_questions = []
        for q in questions:
            q = re.sub(r"\s+", "", q)
            if q and q not in unique_questions:
                unique_questions.append(q)

        for q in unique_questions[:qa_per_article]:
            samples.append({
                "conversations": [
                    {
                        "role": "user",
                        "content": q
                    },
                    {
                        "role": "assistant",
                        "content": answer
                    }
                ]
            })

    return samples


def save_jsonl(items: List[Dict], path: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def save_article_debug(articles: List[Dict], path: str):
    """
    保存条款解析结果，方便人工检查。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for item in articles:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, required=True, help="研究生手册 PDF 路径")
    parser.add_argument("--output", type=str, default="dataset/graduate_handbook_sft.jsonl", help="输出 SFT jsonl 路径")
    parser.add_argument("--debug_output", type=str, default="dataset/graduate_handbook_articles_debug.jsonl", help="条款切分检查文件")
    parser.add_argument("--start_page", type=int, default=1, help="开始页，1-based")
    parser.add_argument("--end_page", type=int, default=None, help="结束页，1-based")
    parser.add_argument("--qa_per_article", type=int, default=3, help="每个条款生成几条问答")
    args = parser.parse_args()

    print(f"读取 PDF: {args.input}")

    lines = extract_lines_from_pdf(
        pdf_path=args.input,
        start_page=args.start_page,
        end_page=args.end_page
    )

    print(f"抽取文本行数: {len(lines)}")

    articles = parse_articles(lines)

    print(f"解析条款数: {len(articles)}")

    if not articles:
        print("没有解析到条款，请检查 PDF 文本是否可复制，或调整正则规则。")
        return

    save_article_debug(articles, args.debug_output)
    print(f"条款检查文件已保存: {args.debug_output}")

    samples = build_sft_samples(
        articles=articles,
        qa_per_article=args.qa_per_article
    )

    save_jsonl(samples, args.output)

    print(f"SFT 数据集已保存: {args.output}")
    print(f"生成问答样本数: {len(samples)}")

    print("\n示例样本：")
    print(json.dumps(samples[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()