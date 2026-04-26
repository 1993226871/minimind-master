import re
import json
import argparse
from pathlib import Path
from typing import List, Dict, Optional

import fitz  # pymupdf
from tqdm import tqdm


CN_NUM = "一二三四五六七八九十百千万零〇0-9"


def normalize_text(s: str) -> str:
    """
    基础清洗。
    """
    if s is None:
        return ""

    s = s.replace("\u3000", " ")
    s = s.replace("\xa0", " ")
    s = s.replace("\r\n", "\n")
    s = s.replace("\r", "\n")

    # 压缩横向空格，但保留换行
    s = re.sub(r"[ \t]+", " ", s)

    # 清理换行前后的空格
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)

    # 合并过多空行
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


def repair_pdf_text(text: str) -> str:
    """
    修复 PDF 抽取导致的常见问题：
    1. “华中科技大学”被拆开；
    2. “第X章”“第X条”粘在上一句后面；
    3. 文件标题粘在上一段后面。
    """
    if text is None:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 修复“华中科技大学”断行
    text = text.replace("华\n中科技大学", "华中科技大学")
    text = text.replace("华 \n中科技大学", "华中科技大学")
    text = text.replace("华　\n中科技大学", "华中科技大学")

    # 修复“根据《\n中科技大学...”这种明显错误
    text = text.replace("根据《\n中科技大学", "根据《华中科技大学")
    text = text.replace("《\n中科技大学", "《华中科技大学")

    # 在“第X章”前补换行，避免粘到上一条后面
    text = re.sub(
        rf"(?<!\n)(第[{CN_NUM}]+章\s*[^\n。；，,]{{1,30}})",
        r"\n\1",
        text,
    )

    # 在“第X条”前补换行，避免多条粘连
    text = re.sub(
        rf"(?<!\n)(第[{CN_NUM}]+条)",
        r"\n\1",
        text,
    )

    # 在常见文件标题前补换行
    # 例如：华中科技大学研究生学籍管理规定
    text = re.sub(
        r"(?<!\n)(华中科技大学[^。\n]{4,60}(?:规定|办法|细则|通知|守则))",
        r"\n\1",
        text,
    )

    # 在“关于……”类文件标题前补换行
    text = re.sub(
        r"(?<!\n)(关于[^。\n]{4,60}(?:通知|规定|办法))",
        r"\n\1",
        text,
    )

    return text


def is_toc_or_cover_page(lines: List[str]) -> bool:
    """
    判断是否为封面/目录页。
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
    不删除文件标题，因为标题需要用于切分。
    """
    cleaned = []

    for line in lines:
        line = line.strip()

        if not line:
            continue

        # 纯页码
        if re.fullmatch(r"\d+", line):
            continue

        # 罗马数字页码
        if re.fullmatch(r"[IVX]+", line):
            continue

        # 常见空白噪声
        if line in {"-", "—", "－"}:
            continue

        cleaned.append(line)

    return cleaned


def clean_doc_title(title: str) -> str:
    """
    清理规章文件标题。
    """
    if title is None:
        return ""

    title = title.replace("\n", "")
    title = re.sub(r"\s+", "", title)

    # 修复缺失“华”的情况
    if title.startswith("中科技大学"):
        title = "华" + title

    # 删除标题后误拼入的页码
    title = re.sub(r"\d+$", "", title)

    # 删除标题前后的无关符号
    title = title.strip(" -—－")

    return title.strip()


def is_doc_title(line: str) -> bool:
    """
    判断是否为一份规章制度文件标题。
    适配：
    华中科技大学博士研究生培养工作规定
    华中科技大学研究生学籍管理实施细则
    华中科技大学学生违纪处分规定
    关于开展博士学位论文重点审核工作的通知
    """
    line = clean_doc_title(line)

    if len(line) < 6 or len(line) > 80:
        return False

    # 排除章节标题
    if re.fullmatch(rf"第[{CN_NUM}]+章.*", line):
        return False

    # 排除发文说明、日期等
    if re.search(r"校研〔|研字〔|经.*审议|二○|202[0-9]年|审批通过|审议通过", line):
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
        "管理规定",
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
    line = line.strip()
    return bool(re.fullmatch(rf"第[{CN_NUM}]+章\s*.+", line))


def is_article_start(line: str) -> bool:
    """
    判断条款开头，如：第一条 ...
    """
    line = line.strip()
    return bool(re.match(rf"^第[{CN_NUM}]+条", line))


def extract_lines_from_pdf(
    pdf_path: str,
    start_page: int = 1,
    end_page: Optional[int] = None,
) -> List[str]:
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

        # 先修复，再做基础清洗
        text = repair_pdf_text(text)
        text = normalize_text(text)

        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # 跳过目录页、封面页
        if is_toc_or_cover_page(lines):
            continue

        # 去掉页眉页脚
        lines = remove_page_noise(lines)

        all_lines.extend(lines)

    return all_lines


def merge_broken_lines(lines: List[str]) -> List[str]:
    """
    PDF 抽取后经常把一个句子拆成多行。
    这里做轻量合并：
    - 文档标题、章节标题、条款开头保留为新行；
    - 普通正文尽量拼接到上一行。
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

                # 如果上一行是文件标题或章节标题，不合并
                if is_doc_title(prev) or is_chapter_title(prev):
                    merged.append(line)
                else:
                    # 中文正文断行直接拼接
                    merged[-1] = prev + line

    return merged


def clean_article_text(text: str) -> str:
    """
    清理条款文本。
    """
    text = normalize_text(text)
    text = text.replace("\n", "")
    text = re.sub(r"\s+", " ", text)

    # 修复明显 PDF 抽取错误
    text = text.replace("根据《 中科技大学", "根据《华中科技大学")
    text = text.replace("根据《中科技大学", "根据《华中科技大学")
    text = text.replace("《 中科技大学", "《华中科技大学")
    text = text.replace("《中科技大学", "《华中科技大学")

    # 删除误拼到条款末尾的下一章标题
    text = re.sub(
        rf"第[{CN_NUM}]+章\s*[^\s。；，,]{{1,30}}$",
        "",
        text,
    )

    # 如果附件内容被拼进条款，截断
    text = re.split(r"(附件\s*\d+|附表\s*\d+|附件：|附\s*件)", text)[0]

    # 去掉多余空格
    text = text.replace(" ，", "，")
    text = text.replace(" 。", "。")
    text = text.replace(" ；", "；")
    text = text.replace(" ：", "：")
    text = text.replace(" 、", "、")

    return text.strip()


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
    current_chapter = ""
    current_article_no = None
    current_article_lines = []

    def flush_article():
        nonlocal current_article_no, current_article_lines

        if current_doc and current_article_no and current_article_lines:
            article_text = "".join(current_article_lines).strip()
            article_text = clean_article_text(article_text)

            if len(article_text) >= 20:
                articles.append(
                    {
                        "doc_title": current_doc,
                        "chapter_title": current_chapter or "",
                        "article_no": current_article_no,
                        "article_text": article_text,
                    }
                )

        current_article_no = None
        current_article_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 新文件标题
        if is_doc_title(line):
            doc_title = clean_doc_title(line)

            # 如果是页眉重复标题，且与当前标题一致，则跳过
            if current_doc == doc_title:
                continue

            flush_article()
            current_doc = doc_title
            current_chapter = ""
            continue

        # 章节标题
        if is_chapter_title(line):
            flush_article()
            current_chapter = re.sub(r"\s+", " ", line).strip()
            continue

        # 条款开头
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


def extract_topic(article_text: str) -> str:
    """
    从条款中粗略抽取主题，生成更自然的问题。
    不依赖大模型，只做规则处理。
    """
    text = re.sub(rf"^第[{CN_NUM}]+条", "", article_text).strip()

    # 优先截取第一个逗号/句号/分号/冒号之前的内容
    seg = re.split(r"[，。；：]", text)[0].strip()

    # 去掉过泛的开头
    seg = re.sub(
        r"^(研究生|博士生|硕士生|学生|新生|学校|培养单位|院（系）|导师)",
        "",
        seg,
    ).strip()
    seg = re.sub(
        r"^(应当|应|须|必须|可以|可|不得|一般|在校期间|在学期间)",
        "",
        seg,
    ).strip()

    if not seg:
        seg = text[:24]

    if len(seg) > 24:
        seg = seg[:24]

    return seg


def build_answer(article: Dict) -> str:
    """
    构造 assistant 回答。
    """
    doc_title = clean_doc_title(article["doc_title"])
    chapter_title = article.get("chapter_title", "")
    article_no = article["article_no"]
    article_text = article["article_text"]

    # 去掉正文开头重复的“第X条”
    article_body = re.sub(rf"^第[{CN_NUM}]+条\s*", "", article_text).strip()

    source = f"根据《{doc_title}》"
    if chapter_title:
        source += f"{chapter_title}"
    source += f"{article_no}"

    answer = (
        f"{source}，{article_body} "
        f"具体办理和执行口径应以学校最新研究生手册、院系通知及相关部门解释为准。"
    )

    # 最后再做一次清洗
    answer = answer.replace("\n", "")
    answer = re.sub(r"\s+", " ", answer)
    answer = answer.replace("《 中科技大学", "《华中科技大学")
    answer = answer.replace("《中科技大学", "《华中科技大学")

    # 避免“第九条，第九条”
    answer = re.sub(
        rf"(根据《[^》]+》.*?第[{CN_NUM}]+条)，第[{CN_NUM}]+条",
        r"\1，",
        answer,
    )

    return answer.strip()


def build_sft_samples(articles: List[Dict], qa_per_article: int = 3) -> List[Dict]:
    """
    将条款转换成 MiniMind SFT conversations 格式。
    """
    samples = []

    for article in articles:
        doc_title = clean_doc_title(article["doc_title"])
        chapter_title = article.get("chapter_title", "")
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
            samples.append(
                {
                    "conversations": [
                        {
                            "role": "user",
                            "content": q,
                        },
                        {
                            "role": "assistant",
                            "content": answer,
                        },
                    ]
                }
            )

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


def validate_samples(samples: List[Dict]) -> List[Dict]:
    """
    检查生成样本中的明显异常。
    """
    bad = []

    for i, obj in enumerate(samples, 1):
        text = json.dumps(obj, ensure_ascii=False)

        reason = []

        if "《\n" in text or "根据《\n" in text:
            reason.append("标题中存在换行")

        if "《中科技大学" in text or "根据《中科技大学" in text:
            reason.append("疑似缺少'华'字")

        if re.search(r"第[一二三四五六七八九十百千万零〇0-9]+章[^，。；]{1,30}具体办理", text):
            reason.append("疑似章节标题拼入条款")

        if re.search(r"第[一二三四五六七八九十百千万零〇0-9]+条，第[一二三四五六七八九十百千万零〇0-9]+条", text):
            reason.append("条款编号重复")

        if "附件1" in text or "附件 1" in text or "附表1" in text:
            reason.append("疑似附件内容拼入样本")

        if reason:
            bad.append(
                {
                    "line": i,
                    "reason": "；".join(reason),
                    "sample": obj,
                }
            )

    return bad


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, required=True, help="研究生手册 PDF 路径")
    parser.add_argument(
        "--output",
        type=str,
        default="dataset/graduate_handbook_sft.jsonl",
        help="输出 SFT jsonl 路径",
    )
    parser.add_argument(
        "--debug_output",
        type=str,
        default="dataset/graduate_handbook_articles_debug.jsonl",
        help="条款切分检查文件",
    )
    parser.add_argument(
        "--bad_output",
        type=str,
        default="dataset/graduate_handbook_bad_samples.jsonl",
        help="异常样本检查文件",
    )
    parser.add_argument("--start_page", type=int, default=1, help="开始页，1-based")
    parser.add_argument("--end_page", type=int, default=None, help="结束页，1-based")
    parser.add_argument("--qa_per_article", type=int, default=3, help="每个条款生成几条问答")
    args = parser.parse_args()

    print(f"读取 PDF: {args.input}")

    lines = extract_lines_from_pdf(
        pdf_path=args.input,
        start_page=args.start_page,
        end_page=args.end_page,
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
        qa_per_article=args.qa_per_article,
    )

    save_jsonl(samples, args.output)

    print(f"SFT 数据集已保存: {args.output}")
    print(f"生成问答样本数: {len(samples)}")

    bad_samples = validate_samples(samples)

    if bad_samples:
        save_jsonl(bad_samples, args.bad_output)
        print(f"发现疑似异常样本数: {len(bad_samples)}")
        print(f"异常样本已保存: {args.bad_output}")
        print("建议先打开该文件人工检查，再用于训练。")
    else:
        print("异常样本检查通过：未发现明显脏样本。")

    print("\n示例样本：")
    print(json.dumps(samples[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()