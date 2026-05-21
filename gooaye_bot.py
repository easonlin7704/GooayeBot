import os
import sys
import json
import time
import datetime
import subprocess
import smtplib
import requests
import feedparser
from time import mktime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from openai import OpenAI

# ==========================================
CONFIG_FILE = "config.json"
STATE_FILE = "last_episode.json"
REPORT_DIR = "reports"
PODCAST_RSS = "https://feeds.soundon.fm/podcasts/954689a5-3096-43a4-a80b-7810b219cef3.xml"
# ==========================================

RSS_SOURCES = [
    "https://tw.stock.yahoo.com/rss?category=news",
    "https://tw.stock.yahoo.com/rss?category=tw-market",
    "https://tw.stock.yahoo.com/rss?category=intl-markets",
    "https://tw.stock.yahoo.com/rss?category=individual",
    "https://tw.stock.yahoo.com/rss?category=research",
    "https://tw.stock.yahoo.com/rss?category=hot",
    "https://www.reuters.com/arc/outboundfeeds/rss/category/business/?outputType=xml",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "https://www.marketwatch.com/rss/topstories",
    "https://www.economist.com/finance-and-economics/rss.xml",
    "https://www.economist.com/business/rss.xml",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.livemint.com/rss/markets",
    "https://www.livemint.com/rss/companies"
]

def load_config():
    # Cloud mode: read from environment variables if present
    if os.environ.get("OPENAI_API_KEY"):
        recipients_raw = os.environ.get("RECIPIENTS", "lin04070221@gmail.com")
        return {
            "openai_api_key":    os.environ["OPENAI_API_KEY"],
            "gmail_user":        os.environ.get("GMAIL_USER", "lin04070221@gmail.com"),
            "gmail_app_password":os.environ.get("GMAIL_APP_PASSWORD", ""),
            "recipients":        [r.strip() for r in recipients_raw.split(",")],
            "model":             os.environ.get("MODEL", "gpt-5.4"),
        }
    # Local mode: read config.json
    if not os.path.exists(CONFIG_FILE):
        default = {
            "openai_api_key": "請填入您的 OpenAI API Key",
            "gmail_user": "lin04070221@gmail.com",
            "gmail_app_password": "請填入 Gmail 應用程式密碼（16碼，不含空格）",
            "recipients": ["lin04070221@gmail.com"],
            "model": "gpt-5.4"
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)
        print(f"⚠️  已建立設定檔 {CONFIG_FILE}，請填入正確的設定後重新執行。")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

def setup_dirs():
    os.makedirs(REPORT_DIR, exist_ok=True)

# ─── Podcast 自動下載 ───────────────────────────────────────────────────────

def _episode_is_recent(published_str, hours=40):
    """True if episode was published within the last N hours (for cloud dedup)."""
    if not published_str:
        return True
    try:
        from email.utils import parsedate_to_datetime
        pub_dt = parsedate_to_datetime(published_str)
        now = datetime.datetime.now(pub_dt.tzinfo)
        return (now - pub_dt).total_seconds() < hours * 3600
    except Exception:
        return True

def get_latest_episode():
    print("📡 正在檢查股癌 Podcast 最新集數...")
    try:
        feed = feedparser.parse(PODCAST_RSS)
        if not feed.entries:
            print("❌ RSS Feed 回應為空")
            return None
        entry = feed.entries[0]
        audio_url = entry.enclosures[0].href if entry.enclosures else None
        return {
            "id": entry.get("id", entry.title),
            "title": entry.title,
            "published": entry.get("published", ""),
            "audio_url": audio_url
        }
    except Exception as e:
        print(f"❌ 無法取得 RSS Feed: {e}")
        return None

def load_last_episode():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_last_episode(ep_data):
    data = {k: ep_data[k] for k in ep_data if k != "audio_url"}
    data["processed_at"] = time.strftime("%Y-%m-%d %H:%M")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def download_episode(url, filename="gooaye.mp3"):
    print(f"⬇️  正在下載音檔...")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; PodcastBot/1.0)"}
        r = requests.get(url, stream=True, timeout=300, headers=headers)
        r.raise_for_status()
        with open(filename, "wb") as f:
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                total += len(chunk)
        size_mb = total / 1024 / 1024
        print(f"✅ 下載完成 ({size_mb:.1f} MB)")
        return filename
    except Exception as e:
        print(f"❌ 下載失敗: {e}")
        return None

# ─── 音檔處理 ───────────────────────────────────────────────────────────────

def compress_audio(input_path):
    print("🔨 正在壓縮音檔...")
    output_path = "gooaye_compressed.mp3"
    if os.path.exists(output_path):
        try: os.remove(output_path)
        except: pass

    script_dir = os.path.dirname(os.path.abspath(__file__))
    win_ffmpeg = os.path.join(script_dir, "ffmpeg.exe")
    ffmpeg_cmd = win_ffmpeg if os.path.exists(win_ffmpeg) else "ffmpeg"

    cmd = [ffmpeg_cmd, "-y", "-i", input_path, "-ac", "1", "-ar", "16000", "-b:a", "48k", output_path]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return output_path
    except Exception as e:
        print(f"❌ 壓縮失敗 ({e})，使用原始檔案")
        return input_path

def transcribe_audio(filepath, client):
    print("👂 正在使用 Whisper 進行轉錄...")
    try:
        with open(filepath, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                prompt="這是一段關於股票投資的 Podcast，內容包含繁體中文、台股、美股代號與金融術語。"
            )
        print(f"✅ 轉錄完成（{len(transcript.text)} 字）")
        return transcript.text
    except Exception as e:
        print(f"❌ 轉錄失敗: {e}")
        return ""

# ─── 新聞抓取與過濾 ─────────────────────────────────────────────────────────

def fetch_rss_news():
    print("📡 正在下載全球財經新聞（16 個來源）...")
    all_entries = []
    now = datetime.datetime.now()
    two_weeks_ago = now - datetime.timedelta(weeks=2)

    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            source_name = url.split('/')[2].replace('www.', '')
            for entry in feed.entries:
                is_recent = True
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        entry_dt = datetime.datetime.fromtimestamp(mktime(entry.published_parsed))
                        if entry_dt < two_weeks_ago:
                            is_recent = False
                    except: pass
                if is_recent:
                    all_entries.append({
                        "title": entry.title,
                        "link": entry.link,
                        "published": entry.get("published", "未知"),
                        "summary": entry.get("summary", ""),
                        "source": source_name
                    })
        except Exception:
            pass

    print(f"✅ 取得 {len(all_entries)} 篇近期新聞")
    return all_entries

def get_ai_search_queries(text, client, model):
    print("🧠 正在分析關鍵字...")
    prompt = f"""
你是一位專業的避險基金經理人。請閱讀這份「完整的 Podcast 逐字稿」，從中選出所有與「股市」、「財經」、「市場趨勢」相關的關鍵字。

⚠️ 關鍵字原則：
1. 只提取與投資、經濟、產業、公司、金融市場相關的詞彙。
2. 不限數量，盡量列出所有相關重要關鍵字。
3. 請將關鍵字拆解為獨立單詞（例如「台積電」和「CoWoS」分開）。
4. 針對重要公司或術語，若有英文名稱請一併列出（如 TSMC、Nvidia）。

Podcast 逐字稿：
{text}

請只回傳 JSON 格式：
{{ "queries": ["關鍵字1", "關鍵字2", ...] }}
"""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("queries", [])
    except Exception as e:
        print(f"關鍵字分析失敗: {e}")
        return []

def filter_news(all_news, queries):
    individual_keywords = set()
    for q in queries:
        parts = q.replace(',', ' ').replace('，', ' ').replace('、', ' ').replace('/', ' ').split()
        for p in parts:
            p = p.strip()
            if len(p) > 1 or (len(p) == 1 and not p.isascii()):
                individual_keywords.add(p)
            elif p.lower() in ['ai', '5g']:
                individual_keywords.add(p)

    matched_results = []
    seen_links = set()
    for kw in individual_keywords:
        for news in all_news:
            content_text = (news['title'] + " " + news['summary']).lower()
            if kw.lower() in content_text and news['link'] not in seen_links:
                matched_results.append(
                    f"- [{news['source']}] {news['title']} ({news['published']})"
                )
                seen_links.add(news['link'])

    if len(matched_results) > 300:
        matched_results = matched_results[:300]
    return "\n".join(matched_results)

# ─── 報告生成 ───────────────────────────────────────────────────────────────

REPORT_SYSTEM_PROMPT = """你是摩根士丹利亞太區首席投資策略分析師，擁有 20 年台美股投資研究經驗。
你的任務是根據 Podcast 逐字稿與最新財經新聞，出具一份機構等級的深度投資研報。

寫作原則：
- 語氣專業、客觀，避免過度主觀情緒
- 觀點必須有邏輯依據，結論要明確可執行
- 嚴格區分「主持人明確說的」與「你的延伸推論」
- 不要在輸出中出現任何格式說明或佔位符文字"""

def generate_final_report(text, search_results, ep_title, client, model):
    print(f"✍️  正在使用 {model} 撰寫分析研報...")
    today = time.strftime("%Y-%m-%d")

    user_prompt = f"""
===== 輸入資料 A：近期全球財經新聞 =====
{search_results}

===== 輸入資料 B：Podcast 完整逐字稿 =====
集數：{ep_title}
{text}

===== 輸出格式要求（嚴格遵守，使用 Markdown） =====

# 執行摘要

本集三大核心訊號（每點一行，精煉至 30 字以內）：
1.
2.
3.

# 一、市場環境與主持人核心觀點

請依以下面向逐點整理主持人對當前市場的判斷：

**多空研判：** （多頭 / 中性偏多 / 中性 / 中性偏空 / 空頭）說明判斷依據。

**資金輪動方向：** 資金從哪流向哪，哪些族群在發動，哪些在退潮。

**整體操作哲學：** 主持人對持股水位、操作頻率、選股邏輯的核心主張。

**本集最重要的產業主軸：** 點出 1-2 個主持人最花時間分析的產業，並說明其為何重要。

# 二、主持人點名個股

僅列出主持人「親口明確點名」的個股，每檔格式如下（不可遺漏，不可新增未點名者）：

### [股票代號] [公司名稱]

- **信心評級：** （依主持人語氣強度評定，[強烈看多] / [看多] / [觀察中] / [中性] / [謹慎]）
- **產業主題：** （例：被動元件漲價 / AI ASIC 供應鏈 / 記憶體景氣循環 / 光通訊 / ...）
- **主持人核心論述：** 精確還原主持人的主要觀點與邏輯，勿過度濃縮，保留關鍵細節。
- **新聞佐證：** 從輸入資料 A 中引用相關新聞標題（格式：「標題」— 來源）；若無則寫「本期新聞暫無直接佐證」。
- **現況評估：** （[過熱慎追] / [趨勢發酵中] / [低度關注尚未爆發] / [震盪整理等待] ）
- **操作參考：** 一句話說明切入思路（如：等拉回至均線附近分批承接 / 趨勢持股不輕易減碼 / 目前觀察，不宜追高）

# 三、延伸推論標的

根據主持人的產業邏輯，推導出未被點名但具受惠邏輯的個股。每檔格式如下：

### [股票代號] [公司名稱]

- **推論強度：** （[強關聯] / [中關聯] / [弱關聯]）
- **產業主題：** 同上
- **受惠邏輯：** 說明為何此股受惠，邏輯鏈要清晰（A → B → C 格式）
- **新聞佐證：** 同上
- **現況評估：** 同上
- **操作參考：** 同上

# 四、整體投資策略

**大盤研判：** （明確標示：強烈做多 / 做多 / 中性 / 偏謹慎 / 防禦）加一句依據說明。

**核心持股建議（3-5 檔）：**

| 標的 | 產業主題 | 建議比重 | 操作策略 |
|------|----------|----------|----------|
| XXXX | ... | 20% | 趨勢持有，不輕易出場 |

**積極型佈局（可承受 20%+ 回撤）：**
說明可積極佈局的標的組合與進場邏輯。

**保守型佈局（下行保護優先）：**
說明適合風險趨避者的核心配置與分散策略。

# 五、關鍵風險提示

1. **最大尾部風險：** 說明若此風險發生，對上述標的的衝擊程度。
2. **短期擾動因素：** 一週內可能造成波動的事件或數據。
3. **本次分析盲點：** 主觀說明本報告可能的侷限性或未充分考慮之處。

---
*本報告基於 Podcast 集數：{ep_title}，分析日期：{today}，由 AI 模型 {model} 輔助生成。*
*資訊僅供參考，不構成任何投資建議或邀約。投資有風險，決策請獨立判斷。*
"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"報告生成失敗: {e}"

# ─── PDF 生成 ────────────────────────────────────────────────────────────────

EMOJI_MAP = {
    '🎙️': '', '📰': '', '🚦': '', '🔥': '[過熱慎追]',
    '🍺': '[趨勢發酵中]', '💎': '[低度關注]', '🦁': '', '🛡️': '',
    '⏳': '', '🧐': '', '🚀': '', '✅': '', '❌': '',
    '⚠️': '', '📂': '', '📡': '', '👂': '', '✍️': '',
    '🎉': '', '⬇️': '', '🔨': '', '📄': '', '📻': '', '⏰': '',
}

NAVY   = (13,  33,  55)
GOLD   = (200, 155, 50)
LGRAY  = (245, 246, 248)
DGRAY  = (80,  80,  80)
WHITE  = (255, 255, 255)
BLACK  = (30,  30,  30)
BLUE   = (15,  52,  96)

def _find_cjk_fonts():
    """Auto-detect CJK font paths on Windows and Linux."""
    candidates = [
        # Windows: Microsoft JhengHei
        (r'C:\Windows\Fonts\msjh.ttc',   r'C:\Windows\Fonts\msjhbd.ttc'),
        # Ubuntu: fonts-noto-cjk (apt install fonts-noto-cjk)
        ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
         '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'),
        ('/usr/share/fonts/truetype/noto/NotoSansCJKtc-Regular.otf',
         '/usr/share/fonts/truetype/noto/NotoSansCJKtc-Bold.otf'),
        ('/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
         '/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc'),
    ]
    for normal, bold in candidates:
        if os.path.exists(normal):
            return normal, bold if os.path.exists(bold) else normal
    raise FileNotFoundError(
        "找不到 CJK 字型。Windows 請確認 msjh.ttc 存在；"
        "Linux 請執行 sudo apt-get install -y fonts-noto-cjk"
    )

FONT_NORMAL, FONT_BOLD = 'placeholder', 'placeholder'  # resolved at runtime

def _clean_for_pdf(text):
    import re
    for emoji, replacement in EMOJI_MAP.items():
        text = text.replace(emoji, replacement)
    text = re.sub(r'[\U00010000-\U0010FFFF]', '', text)
    text = re.sub(r'[︎️️︎]', '', text)
    return text

def _strip_inline_md(text):
    """Remove markdown inline markers for plain rendering."""
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    return text.strip()

def _write_bold_line(pdf, line, font_size=10, line_h=6):
    """Write a line that may contain **bold** segments."""
    import re
    parts = re.split(r'(\*\*.*?\*\*)', line)
    for part in parts:
        if part.startswith('**') and part.endswith('**'):
            pdf.set_font('CJK', 'B', font_size)
            pdf.write(line_h, part[2:-2])
        else:
            pdf.set_font('CJK', '', font_size)
            pdf.write(line_h, part)

def convert_to_pdf(md_content, output_path):
    import re
    from fpdf import FPDF, XPos, YPos

    try:
        font_normal, font_bold = _find_cjk_fonts()
        cleaned = _clean_for_pdf(md_content)

        # ── PDF class with header/footer ────────────────────────────────────
        class ReportPDF(FPDF):
            ep_title  = ''
            date_str  = ''

            def header(self):
                if self.page_no() <= 1:
                    return
                self.set_font('CJK', '', 8)
                self.set_text_color(*DGRAY)
                self.set_x(self.l_margin)
                self.cell(0, 5, f'股癌 Podcast  AI 投資研報  {self.date_str}',
                          new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                self.set_draw_color(210, 210, 210)
                self.line(self.l_margin, self.get_y(),
                          self.w - self.r_margin, self.get_y())
                self.ln(3)
                self.set_text_color(*BLACK)

            def footer(self):
                if self.page_no() <= 1:
                    return
                self.set_y(-13)
                self.set_font('CJK', '', 8)
                self.set_text_color(*DGRAY)
                self.cell(0, 5,
                          f'第 {self.page_no()} 頁   |   本報告由 AI 輔助生成，僅供參考，不構成投資建議',
                          align='C')

        pdf = ReportPDF(orientation='P', unit='mm', format='A4')
        pdf.add_font('CJK',              fname=font_normal)
        pdf.add_font('CJK', style='B',  fname=font_bold)
        pdf.add_font('CJK', style='I',  fname=font_normal)
        pdf.add_font('CJK', style='BI', fname=font_bold)
        pdf.set_auto_page_break(auto=True, margin=20)

        # Extract episode title & date from footer line
        date_match = re.search(r'分析日期：(\d{4}-\d{2}-\d{2})', md_content)
        ep_match   = re.search(r'集數：(EP\d+[^\，,，\n]*)', md_content)
        pdf.date_str  = date_match.group(1) if date_match else time.strftime('%Y-%m-%d')
        pdf.ep_title  = ep_match.group(1).strip()   if ep_match  else ''

        # ── Cover page ──────────────────────────────────────────────────────
        pdf.add_page()
        pdf.set_margins(0, 0, 0)

        # Dark background
        pdf.set_fill_color(*NAVY)
        pdf.rect(0, 0, pdf.w, pdf.h, 'F')

        # Gold top bar
        pdf.set_fill_color(*GOLD)
        pdf.rect(0, 52, pdf.w, 3, 'F')

        # Main title
        pdf.set_y(65)
        pdf.set_font('CJK', 'B', 30)
        pdf.set_text_color(*WHITE)
        pdf.cell(0, 16, '股癌 Podcast', align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font('CJK', 'B', 20)
        pdf.set_text_color(*GOLD)
        pdf.cell(0, 12, 'AI 深度投資研報', align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(10)

        # Episode
        pdf.set_font('CJK', '', 14)
        pdf.set_text_color(200, 215, 230)
        ep_display = pdf.ep_title if len(pdf.ep_title) < 36 else pdf.ep_title[:33] + '...'
        pdf.cell(0, 9, ep_display, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

        # Date
        pdf.set_font('CJK', '', 12)
        pdf.set_text_color(140, 170, 200)
        pdf.cell(0, 8, pdf.date_str, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(50)

        # Divider line
        pdf.set_fill_color(*GOLD)
        pdf.rect(0, pdf.h - 52, pdf.w, 2, 'F')

        # Disclaimer
        pdf.set_y(pdf.h - 42)
        pdf.set_font('CJK', '', 9)
        pdf.set_text_color(110, 120, 130)
        pdf.set_x(20)
        pdf.multi_cell(pdf.w - 40, 6,
                       '本報告由人工智慧模型輔助生成，內容僅供參考，不構成任何投資建議或邀約。\n'
                       '投資有風險，入市前請獨立評估，自行承擔決策責任。',
                       align='C')

        # ── Content pages ───────────────────────────────────────────────────
        pdf.add_page()
        pdf.set_margins(18, 20, 18)

        lines = cleaned.split('\n')
        i = 0
        while i < len(lines):
            raw = lines[i]
            line = raw.strip()

            # --- H1: navy bar with white text ---
            if line.startswith('# ') and not line.startswith('## '):
                title = line[2:].strip()
                pdf.ln(4)
                pdf.set_fill_color(*NAVY)
                pdf.set_text_color(*WHITE)
                pdf.set_font('CJK', 'B', 13)
                pdf.cell(0, 11, f'  {title}',
                         fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(4)
                pdf.set_text_color(*BLACK)

            # --- H2: gold underline ---
            elif line.startswith('## '):
                title = line[3:].strip()
                pdf.ln(3)
                pdf.set_font('CJK', 'B', 12)
                pdf.set_text_color(*NAVY)
                pdf.cell(0, 8, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                y = pdf.get_y()
                pdf.set_draw_color(*GOLD)
                pdf.set_line_width(0.8)
                pdf.line(pdf.l_margin, y, pdf.l_margin + 55, y)
                pdf.set_line_width(0.2)
                pdf.ln(3)
                pdf.set_text_color(*BLACK)

            # --- H3: light blue box (stock entry) ---
            elif line.startswith('### '):
                title = line[4:].strip()
                pdf.ln(3)
                pdf.set_fill_color(*LGRAY)
                pdf.set_text_color(*BLUE)
                pdf.set_font('CJK', 'B', 11)
                pdf.cell(0, 9, f'  {title}',
                         fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(*BLACK)
                pdf.ln(1)

            # --- Horizontal rule ---
            elif line == '---':
                pdf.ln(2)
                pdf.set_draw_color(200, 200, 200)
                pdf.line(pdf.l_margin, pdf.get_y(),
                         pdf.w - pdf.r_margin, pdf.get_y())
                pdf.ln(4)

            # --- Table row ---
            elif line.startswith('|') and line.endswith('|'):
                cells = [c.strip() for c in line.strip('|').split('|')]
                if all(set(c) <= set('-: ') for c in cells):
                    i += 1
                    continue
                col_w = (pdf.w - pdf.l_margin - pdf.r_margin) / max(len(cells), 1)
                is_header = (i > 0 and lines[i-1].strip().startswith('|'))
                if not is_header:
                    pdf.set_fill_color(*NAVY)
                    pdf.set_text_color(*WHITE)
                    pdf.set_font('CJK', 'B', 9)
                else:
                    pdf.set_fill_color(*LGRAY)
                    pdf.set_text_color(*BLACK)
                    pdf.set_font('CJK', '', 9)
                for c in cells:
                    pdf.cell(col_w, 7, c, border=1,
                             fill=not is_header, new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.ln(7)
                pdf.set_text_color(*BLACK)

            # --- Bullet / list item ---
            elif re.match(r'^[-*]\s', line) or re.match(r'^\d+\.\s', line):
                # Determine indent level
                indent = len(raw) - len(raw.lstrip())
                is_numbered = re.match(r'^\d+\.\s', line)
                content = re.sub(r'^[-*\d.]\s+', '', line)
                x_offset = 6 + indent * 0.5
                pdf.set_x(pdf.l_margin + x_offset)
                # Bullet symbol
                pdf.set_font('CJK', 'B', 10)
                pdf.set_text_color(*GOLD)
                bullet = f'{line.split(".")[0]}.' if is_numbered else '·'
                pdf.cell(5, 6, bullet, new_x=XPos.RIGHT, new_y=YPos.TOP)
                pdf.set_text_color(*BLACK)
                # Content
                avail_w = pdf.w - pdf.r_margin - pdf.get_x()
                parts = re.split(r'(\*\*.*?\*\*)', content)
                start_x = pdf.get_x()
                start_y = pdf.get_y()
                plain = _strip_inline_md(content)
                pdf.set_font('CJK', '', 10)
                pdf.multi_cell(avail_w, 6, plain,
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # --- Blockquote ---
            elif line.startswith('> '):
                content = _strip_inline_md(line[2:])
                pdf.ln(1)
                pdf.set_fill_color(250, 248, 235)
                x0 = pdf.l_margin + 4
                y0 = pdf.get_y()
                pdf.set_x(x0 + 5)
                pdf.set_font('CJK', 'I', 10)
                pdf.set_text_color(*DGRAY)
                pdf.multi_cell(pdf.w - pdf.r_margin - x0 - 5, 6, content,
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                y1 = pdf.get_y()
                pdf.set_fill_color(*GOLD)
                pdf.rect(x0, y0, 2, y1 - y0, 'F')
                pdf.set_text_color(*BLACK)
                pdf.ln(1)

            # --- Empty line ---
            elif line == '':
                pdf.ln(2)

            # --- Regular paragraph ---
            else:
                plain = _strip_inline_md(line)
                if plain:
                    pdf.set_font('CJK', '', 10)
                    pdf.set_text_color(40, 40, 40)
                    pdf.multi_cell(0, 6, plain,
                                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_text_color(*BLACK)

            i += 1

        pdf.output(output_path)
        size_kb = os.path.getsize(output_path) / 1024
        print(f"✅ PDF 生成完成（{size_kb:.0f} KB）: {output_path}")
        return True

    except Exception as e:
        print(f"❌ PDF 生成失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

# ─── 寄送 Email ──────────────────────────────────────────────────────────────

def send_email(subject, body, pdf_path, config):
    recipients = config.get("recipients", [config["gmail_user"]])
    print(f"📧 正在寄送報告至 {recipients}...")
    try:
        msg = MIMEMultipart()
        msg['From'] = config['gmail_user']
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        with open(pdf_path, 'rb') as f:
            attach = MIMEApplication(f.read(), _subtype='pdf')
            attach.add_header(
                'Content-Disposition', 'attachment',
                filename=os.path.basename(pdf_path)
            )
            msg.attach(attach)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(config['gmail_user'], config['gmail_app_password'])
            server.send_message(msg)
        print("✅ 郵件寄送成功！")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ Gmail 認證失敗，請確認 config.json 中的應用程式密碼是否正確。")
        return False
    except Exception as e:
        print(f"❌ 郵件寄送失敗: {e}")
        return False

# ─── 清理暫存 ────────────────────────────────────────────────────────────────

def cleanup():
    for f in ["gooaye.mp3", "gooaye_compressed.mp3"]:
        if os.path.exists(f):
            try: os.remove(f)
            except: pass

# ─── 主程式 ──────────────────────────────────────────────────────────────────

def run():
    # 切換工作目錄到腳本所在位置
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    setup_dirs()

    config = load_config()
    client = OpenAI(api_key=config["openai_api_key"])
    model = config.get("model", "gpt-4o")

    print("=" * 50)
    print("🚀 股癌分析機器人（全自動版 v2.0）")
    print(f"⏰ 執行時間：{time.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 1. 取得最新集數資訊
    ep = get_latest_episode()
    if not ep or not ep["audio_url"]:
        print("❌ 無法取得最新集數，結束。")
        return

    print(f"📻 最新集數：{ep['title']}（{ep['published']}）")

    # 2. 檢查是否需要處理
    is_cloud = bool(os.environ.get("OPENAI_API_KEY"))  # cloud = env var mode
    if is_cloud:
        # Cloud: stateless, check by publication recency (within 40h)
        if not _episode_is_recent(ep["published"], hours=40):
            print(f"⏭️  此集發布已超過 40 小時，本次排程無新集數，跳過。")
            return
    else:
        # Local: check last_episode.json
        last = load_last_episode()
        if last.get("id") == ep["id"]:
            print(f"✅ 此集已於 {last.get('processed_at', '先前')} 處理完畢，無需重複執行。")
            return

    # 3. 下載音檔
    audio_file = download_episode(ep["audio_url"])
    if not audio_file:
        print("❌ 音檔下載失敗，結束。")
        return

    # 4. 壓縮 + 轉錄
    compressed = compress_audio(audio_file)
    transcript = transcribe_audio(compressed, client)
    if not transcript:
        cleanup()
        return

    # 5. 抓取全球財經新聞
    all_news = fetch_rss_news()
    queries = get_ai_search_queries(transcript, client, model)
    market_info = "本次無相關新聞。"
    if queries and all_news:
        result = filter_news(all_news, queries)
        market_info = result if result else "本次搜尋無匹配新聞。"

    # 6. 生成分析報告
    report_content = generate_final_report(transcript, market_info, ep["title"], client, model)

    final_output = report_content

    # 7. 存 Markdown
    ts = time.strftime('%Y%m%d_%H%M')
    safe_title = ep['title'].replace('|', '').replace('/', '').strip()
    md_path = f"{REPORT_DIR}/研報_{ts}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_output)
    print(f"📄 Markdown 存檔：{md_path}")

    # 8. 轉 PDF
    pdf_path = f"{REPORT_DIR}/研報_{ts}.pdf"
    pdf_ok = convert_to_pdf(final_output, pdf_path)

    # 9. 寄送 Email
    if pdf_ok and os.path.exists(pdf_path):
        subject = f"【股癌研報】{ep['title']} — {time.strftime('%Y/%m/%d')}"
        body = (
            f"您好，\n\n"
            f"本週股癌 Podcast 分析研報已自動產生，請見附件 PDF。\n\n"
            f"集數：{ep['title']}\n"
            f"發布日期：{ep['published']}\n"
            f"分析模型：{model}\n"
            f"執行時間：{time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"此郵件由自動排程系統發送。"
        )
        send_email(subject, body, pdf_path, config)
    else:
        print("⚠️  PDF 不存在，跳過寄送。")

    # 10. 記錄已處理（防止重複執行）
    save_last_episode(ep)

    # 11. 清理暫存音檔
    cleanup()

    print(f"\n🎉 全部完成！報告存於 {REPORT_DIR}/")

if __name__ == "__main__":
    run()
