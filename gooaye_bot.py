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

# ─── HTML 報告生成 ───────────────────────────────────────────────────────────

GITHUB_PAGES_BASE = "https://easonlin7704.github.io/GooayeBot"
DOCS_DIR = "docs/reports"

_EMOJI_STRIP = {
    '🎙️': '', '📰': '', '🚦': '', '🔥': '[過熱慎追]',
    '🍺': '[趨勢發酵中]', '💎': '[低度關注]', '🦁': '', '🛡️': '',
    '⏳': '', '🧐': '', '🚀': '', '✅': '', '❌': '',
    '⚠️': '', '📂': '', '📡': '', '👂': '', '✍️': '',
    '🎉': '', '⬇️': '', '🔨': '', '📄': '', '📻': '', '⏰': '',
}

def _clean_text(text):
    import re
    for emoji, rep in _EMOJI_STRIP.items():
        text = text.replace(emoji, rep)
    text = re.sub(r'[\U00010000-\U0010FFFF]', '', text)
    return text

def _strip_inline_md(text):
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    return text.strip()

def _extract_summary_data(md_content):
    """Parse key sections from report markdown for the one-page summary."""
    import re
    data = {
        'signals': [], 'market_view': '',
        'stocks': [], 'core_holdings': [], 'risks': [],
    }
    section = ''
    for line in md_content.split('\n'):
        s = line.strip()
        if re.match(r'^#{1,2}\s+[^#]', s):
            section = re.sub(r'^#+\s+', '', s).strip()
            continue
        # 執行摘要 — numbered signals (handles "1. 1. ..." double-numbering from GPT)
        if '執行摘要' in section:
            m = re.match(r'^\d+[\.\)]\s*\d*[\.\)]?\s*(.+)', s)
            if m and len(data['signals']) < 3:
                sig = _strip_inline_md(m.group(1)).strip()
                if sig:
                    data['signals'].append(sig)
        # 二、主持人點名個股
        if '點名個股' in section:
            if s.startswith('### '):
                m = re.match(r'\[(.+?)\]\s*(.+)', s[4:].strip())
                if m:
                    data['stocks'].append({'code': m.group(1), 'name': m.group(2).strip(), 'rating': '', 'theme': ''})
            elif data['stocks']:
                if '信心評級' in s:
                    m = re.search(r'\[(.+?)\]', s)
                    if m: data['stocks'][-1]['rating'] = m.group(1)
                elif '產業主題' in s and '：' in s:
                    data['stocks'][-1]['theme'] = _strip_inline_md(s.split('：', 1)[1]).strip()
        # 四、整體投資策略
        if '整體投資策略' in section:
            if '大盤研判' in s and not data['market_view']:
                clean_s = re.sub(r'\*+', '', s)
                m = re.search(r'大盤研判[：:]\s*[（(]?([^\s）)，,。\n]{1,8})', clean_s)
                if m: data['market_view'] = m.group(1).strip('（(）) ')
            if s.startswith('|') and s.endswith('|'):
                cells = [c.strip() for c in s.strip('|').split('|')]
                if (not all(set(c) <= set('-: ') for c in cells)
                        and cells[0] not in ('標的', '') and len(cells) >= 3):
                    data['core_holdings'].append(cells[:4])
        # 五、關鍵風險提示
        if '關鍵風險' in section:
            # Format 1: "1. **標題：** 描述文字"
            m = re.match(r'^\d+[\.\)]\s*\*\*(.+?)\*\*[：:]?\s*(.*)', s)
            if m and len(data['risks']) < 3:
                label = _strip_inline_md(m.group(1).rstrip('：:'))
                desc = _strip_inline_md(m.group(2))
                data['risks'].append(f"{label}：{desc[:42]}" if desc else label)
            # Format 2: "### 標題" (GPT 有時改用 H3)
            elif s.startswith('### ') and len(data['risks']) < 3:
                data['risks'].append(s[4:].strip())
    return data


HTML_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC", "Noto Sans TC", sans-serif; background: #eef1f5; color: #1a1a2e; line-height: 1.75; font-size: 15px; }
.cover { background: linear-gradient(160deg, #0d2137 60%, #1a3a5c); border-bottom: 4px solid #c89b32; }
.cover-inner { max-width: 860px; margin: 0 auto; padding: 52px 32px 44px; text-align: center; }
.cover h1 { font-size: 2em; font-weight: 700; letter-spacing: 3px; color: white; margin-bottom: 8px; }
.cover-subtitle { color: #c89b32; font-size: 1.25em; font-weight: 600; margin-bottom: 24px; }
.cover-ep { color: #b0c8de; font-size: 0.95em; margin-bottom: 4px; }
.cover-date { color: #7a9ab5; font-size: 0.85em; margin-bottom: 28px; }
.cover-disclaimer { font-size: 0.78em; color: #4a6a85; line-height: 1.6; }
.container { max-width: 860px; margin: 0 auto; padding: 28px 24px 60px; }
.summary-card { background: white; border-radius: 8px; border: 1px solid #d8dde8; box-shadow: 0 2px 12px rgba(0,0,0,.07); margin-bottom: 28px; overflow: hidden; }
.summary-header { background: #0d2137; color: white; padding: 12px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.summary-title { font-size: 1em; font-weight: 700; letter-spacing: 1px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: .82em; font-weight: 700; }
.badge-bull { background: #c89b32; color: white; }
.badge-bear { background: #c0392b; color: white; }
.badge-neutral { background: #5a6a7a; color: white; }
.summary-grid { display: grid; grid-template-columns: 1fr 1fr; }
.summary-section { padding: 16px 20px; border-right: 1px solid #e8eaf0; border-bottom: 1px solid #e8eaf0; }
.summary-section:nth-child(even) { border-right: none; }
.signals-section { grid-column: 1 / -1; border-right: none; }
.risks-section { grid-column: 1 / -1; border-right: none; border-bottom: none; }
.summary-section h4 { color: #0d2137; font-size: .8em; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; padding-bottom: 4px; border-bottom: 2px solid #c89b32; display: inline-block; }
.signals-section ol { display: flex; gap: 10px; flex-wrap: wrap; list-style: none; counter-reset: sig; }
.signals-section li { counter-increment: sig; flex: 1 1 200px; background: #f5f7fb; border-left: 3px solid #c89b32; padding: 6px 10px; border-radius: 0 4px 4px 0; font-size: .88em; color: #2c2c3e; }
.signals-section li::before { content: counter(sig) ". "; font-weight: 700; color: #c89b32; }
.summary-table { width: 100%; border-collapse: collapse; font-size: .83em; }
.summary-table th { background: #f0f3f8; color: #0d2137; padding: 5px 8px; text-align: left; font-weight: 600; }
.summary-table td { padding: 4px 8px; border-bottom: 1px solid #eef0f5; color: #2c2c3e; }
.summary-table .code { font-weight: 700; color: #0f3460; }
.r-sbull { color: #c0392b; font-weight: 700; } .r-bull { color: #b8860b; font-weight: 700; }
.r-watch { color: #2980b9; } .r-neutral { color: #7f8c8d; } .r-bear { color: #884400; }
.weight { font-weight: 700; color: #c89b32; }
.risks-section ul { padding-left: 0; display: flex; flex-wrap: wrap; gap: 6px 20px; }
.risks-section li { list-style: none; font-size: .88em; color: #2c2c3e; padding-left: 14px; position: relative; }
.risks-section li::before { content: "▸"; color: #c89b32; position: absolute; left: 0; font-weight: 700; }
.report-content { background: white; border-radius: 8px; border: 1px solid #d8dde8; padding: 28px 32px; box-shadow: 0 2px 12px rgba(0,0,0,.07); }
.report-content h1 { background: #0d2137; color: white; padding: 10px 16px; margin: 28px -32px 18px; font-size: 1.05em; letter-spacing: .5px; }
.report-content h1:first-child { margin-top: -28px; border-radius: 7px 7px 0 0; }
.report-content h2 { color: #0d2137; font-size: 1em; margin: 20px 0 10px; padding-bottom: 4px; border-bottom: 2px solid #c89b32; display: inline-block; }
.report-content h3 { background: #f5f7fb; color: #0f3460; padding: 7px 14px; margin: 16px 0 10px; font-size: .98em; border-left: 3px solid #c89b32; border-radius: 0 4px 4px 0; }
.report-content p { margin: 8px 0; color: #2c2c3e; font-size: .94em; }
.report-content ul, .report-content ol { padding-left: 22px; margin: 8px 0; }
.report-content li { margin: 4px 0; color: #2c2c3e; font-size: .92em; }
.report-content li::marker { color: #c89b32; }
.report-content strong { color: #0d2137; font-weight: 600; }
.report-content em { color: #5a6a7a; }
.report-content blockquote { border-left: 3px solid #c89b32; background: #fdfbf3; padding: 10px 16px; margin: 12px 0; color: #555; border-radius: 0 4px 4px 0; }
.report-content table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: .88em; }
.report-content thead tr { background: #0d2137; color: white; }
.report-content thead th { padding: 9px 12px; text-align: left; font-weight: 600; }
.report-content tbody tr:nth-child(even) { background: #f5f7fb; }
.report-content tbody tr:hover { background: #eef2f7; }
.report-content td { padding: 7px 12px; border-bottom: 1px solid #e0e4ea; color: #2c2c3e; }
.report-content hr { border: none; border-top: 1px solid #d8dde8; margin: 24px 0; }
.page-footer { text-align: center; color: #8a9ab5; font-size: .8em; margin-top: 28px; padding: 16px; }

/* ── Layout ──────────────────────────────────────────────────── */
.page-outer { max-width: 1120px; margin: 0 auto; padding: 0 16px; }
.page-flex  { display: flex; gap: 24px; align-items: flex-start; padding: 28px 0 60px; }
.toc-col    { width: 176px; flex-shrink: 0; position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto; }
.main-col   { flex: 1; min-width: 0; }

/* ── Desktop TOC ─────────────────────────────────────────────── */
.toc-box    { background: white; border-radius: 8px; border: 1px solid #d8dde8; padding: 14px 12px; box-shadow: 0 2px 8px rgba(0,0,0,.05); }
.toc-ttl    { font-size: .72em; font-weight: 700; color: #0d2137; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #eef0f5; }
.toc-list   { list-style: none; padding: 0; margin: 0; }
.toc-list a { display: block; padding: 5px 8px; font-size: .8em; color: #5a6a7a; text-decoration: none; border-left: 2px solid transparent; border-radius: 0 3px 3px 0; transition: all .15s; line-height: 1.4; }
.toc-list a:hover, .toc-list a.tac { color: #0d2137; border-left-color: #c89b32; font-weight: 600; background: #f5f7fb; }

/* ── Mobile TOC ──────────────────────────────────────────────── */
.toc-mob  { display: none; position: sticky; top: 0; z-index: 50; background: white; border-bottom: 2px solid #c89b32; overflow-x: auto; scrollbar-width: none; }
.toc-mob::-webkit-scrollbar { display: none; }
.toc-mob ul { display: flex; list-style: none; padding: 0 12px; margin: 0; white-space: nowrap; }
.toc-mob a  { display: inline-block; padding: 9px 14px; font-size: .8em; color: #5a6a7a; text-decoration: none; border-bottom: 2px solid transparent; transition: all .15s; }
.toc-mob a:hover, .toc-mob a.tac { color: #0d2137; border-bottom-color: #c89b32; font-weight: 600; }

/* ── Stock cards ─────────────────────────────────────────────── */
.stock-card { border: 1px solid #d8dde8; border-radius: 6px; margin: 14px 0; overflow: hidden; }
.stock-card > summary { list-style: none; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 11px 16px; background: #f5f7fb; cursor: pointer; user-select: none; transition: background .15s; }
.stock-card > summary::-webkit-details-marker { display: none; }
.stock-card > summary:hover { background: #eef2f7; }
.stock-card[open] > summary { background: #0d2137; }
.stock-card[open] .sc-code { color: #c89b32; }
.stock-card[open] .sc-name { color: white; }
.stock-card[open] .sc-theme { color: #8ab0cc; background: rgba(255,255,255,.08); }
.stock-card[open] .sc-chev { transform: rotate(180deg); color: #c89b32; }
.sc-code  { font-weight: 700; color: #0f3460; font-size: .86em; font-family: monospace; }
.sc-name  { font-weight: 600; color: #1a1a2e; font-size: .95em; }
.sc-theme { font-size: .74em; color: #5a6a7a; background: #e8edf5; padding: 2px 9px; border-radius: 10px; }
.sc-right { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.sc-rtxt  { font-size: .8em; font-weight: 700; }
.sc-bars  { display: flex; gap: 2px; align-items: center; }
.cb-dot   { display: inline-block; width: 9px; height: 9px; border-radius: 2px; }
.sc-chev  { font-size: .65em; color: #8a9ab5; transition: transform .2s; margin-left: 4px; }
.sc-body  { padding: 16px 20px; background: white; border-top: 1px solid #e8eaf0; }
.sc-body ul { padding-left: 0; list-style: none; margin: 0; }
.sc-body li { padding: 6px 0; border-bottom: 1px solid #f5f7fb; font-size: .9em; color: #2c2c3e; display: flex; flex-wrap: wrap; gap: 4px; }
.sc-body li:last-child { border-bottom: none; }
.sc-body li strong { color: #0d2137; min-width: 88px; flex-shrink: 0; }

/* ── Holdings bar chart (report body) ───────────────────────── */
.hc-chart { margin: 12px 0 20px; }
.hc-row   { display: grid; grid-template-columns: 160px 1fr; gap: 10px; align-items: center; padding: 5px 0; border-bottom: 1px solid #f0f3f8; }
.hc-row:last-child { border-bottom: none; }
.hc-name  { display: block; font-weight: 700; font-size: .86em; color: #0f3460; }
.hc-theme { display: block; font-size: .72em; color: #7a9ab5; margin-top: 1px; }
.hc-track { position: relative; background: #f0f3f8; border-radius: 3px; height: 24px; }
.hc-fill  { height: 100%; background: linear-gradient(90deg, #0d2137, #1e4a6e); border-radius: 3px; }
.hc-lbl   { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); font-weight: 700; font-size: .82em; color: #0d2137; }

/* ── Summary card holdings bar chart ────────────────────────── */
.shc-row   { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.shc-name  { font-size: .82em; font-weight: 700; color: #0f3460; width: 96px; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.shc-track { flex: 1; background: #e8edf5; border-radius: 3px; height: 16px; position: relative; }
.shc-fill  { height: 100%; background: linear-gradient(90deg, #0d2137, #1e4a6e); border-radius: 3px; }
.shc-pct   { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); font-size: .72em; font-weight: 700; color: #0d2137; }

/* ── Responsive ──────────────────────────────────────────────── */
@media (max-width: 900px) {
  .toc-col { display: none; }
  .toc-mob { display: block; }
  .page-flex { padding: 20px 0 40px; }
}
@media (max-width: 620px) {
  .summary-grid { grid-template-columns: 1fr; }
  .summary-section { border-right: none; }
  .report-content { padding: 18px 16px; }
  .report-content h1 { margin-left: -16px; margin-right: -16px; }
  .hc-row { grid-template-columns: 1fr; }
  .sc-body li { flex-direction: column; }
  .sc-body li strong { min-width: auto; }
}
"""

# ─── JavaScript 互動功能 ──────────────────────────────────────────────────────

JS_BLOCK = """<script>
(function(){
'use strict';

const RATINGS = ['強烈看多','看多','觀察中','中性','謹慎'];
const R_COL = {'強烈看多':'#c0392b','看多':'#b8860b','觀察中':'#2980b9','中性':'#7f8c8d','謹慎':'#884400'};
const R_LV  = {'強烈看多':5,'看多':4,'觀察中':3,'中性':2,'謹慎':1};

function convBar(r){
  const lv=R_LV[r]||0, col=R_COL[r]||'#ccc';
  return Array.from({length:5},(_,i)=>
    `<span class="cb-dot" style="background:${i<lv?col:'#dde2ea'}"></span>`
  ).join('');
}

/* ── 1. Stock cards ──────────────────────────────────────────── */
const report = document.getElementById('report');
if(report){
  [...report.querySelectorAll('h3')].forEach(h3=>{
    const m = h3.textContent.match(/^\[(.+?)\]\s*(.+)/);
    if(!m) return;
    const [,code,name] = m;

    const sibs=[];
    let el=h3.nextElementSibling;
    while(el && !['H1','H2','H3'].includes(el.tagName)){ sibs.push(el); el=el.nextElementSibling; }

    let rating='', theme='';
    sibs.forEach(s=>{
      const t=s.textContent;
      if(!rating && t.includes('信心評級')) for(const r of RATINGS) if(t.includes(r)){rating=r;break;}
      if(!theme  && t.includes('產業主題')){
        const tm=t.match(/產業主題[：:]\s*([^；;。\n\r]{2,25})/);
        if(tm) theme=tm[1].replace(/[\[\]【】（）()]/g,'').trim();
      }
    });

    const card=document.createElement('details');
    card.className='stock-card';
    const rHtml=rating?`<span class="sc-rtxt" style="color:${R_COL[rating]}">${rating}</span><span class="sc-bars">${convBar(rating)}</span>`:'';
    const tHtml=theme?`<span class="sc-theme">${theme}</span>`:'';
    card.innerHTML=`<summary><span class="sc-code">[${code}]</span><span class="sc-name">${name}</span>${tHtml}<span class="sc-right">${rHtml}<span class="sc-chev">▼</span></span></summary><div class="sc-body"></div>`;
    sibs.forEach(s=>card.querySelector('.sc-body').appendChild(s));
    h3.parentNode.insertBefore(card,h3); h3.remove();
  });
}

/* ── 2. Holdings bar chart ───────────────────────────────────── */
if(report){
  report.querySelectorAll('table').forEach(tbl=>{
    const ths=[...tbl.querySelectorAll('thead th')].map(t=>t.textContent.trim());
    const pi=ths.findIndex(t=>t.includes('比重'));
    if(pi<0) return;
    const rows=[...tbl.querySelectorAll('tbody tr')];
    if(!rows.length) return;
    const chart=document.createElement('div');
    chart.className='hc-chart';
    rows.forEach(tr=>{
      const c=[...tr.querySelectorAll('td')].map(td=>td.textContent.trim());
      if(!c[0]) return;
      const pn=parseFloat(c[pi])||0;
      chart.innerHTML+=`<div class="hc-row"><div><span class="hc-name">${c[0]}</span><span class="hc-theme">${c[1]||''}</span></div><div class="hc-track"><div class="hc-fill" style="width:${Math.min(pn,100)}%"></div><span class="hc-lbl">${c[pi]}</span></div></div>`;
    });
    tbl.parentNode.insertBefore(chart,tbl); tbl.remove();
  });
}

/* ── 3. TOC ──────────────────────────────────────────────────── */
const tocD=document.getElementById('toc-d');
const tocM=document.getElementById('toc-m');
if(report && tocD){
  const heads=[...report.querySelectorAll('h1')];
  heads.forEach((h,i)=>{
    h.id='s'+i;
    const label=h.textContent.replace(/^[零一二三四五六七八九十]+[、.．]\s*/,'').replace(/^[一-龥]+、/,'').trim();
    [tocD,tocM].forEach(ul=>{
      if(!ul) return;
      const li=document.createElement('li');
      li.innerHTML=`<a href="#s${i}">${label}</a>`;
      ul.appendChild(li);
    });
  });

  let ticking=false;
  window.addEventListener('scroll',()=>{
    if(ticking) return; ticking=true;
    requestAnimationFrame(()=>{
      const sy=window.scrollY+130;
      let active=heads[0];
      heads.forEach(h=>{ if(h.offsetTop<=sy) active=h; });
      if(active){
        document.querySelectorAll('.toc-list a,.toc-mob a').forEach(a=>{
          a.classList.toggle('tac', a.getAttribute('href')==='#'+active.id);
        });
      }
      ticking=false;
    });
  },{passive:true});
}

/* ── 4. Smooth scroll ────────────────────────────────────────── */
document.querySelectorAll('a[href^="#"]').forEach(a=>{
  a.addEventListener('click',e=>{
    const t=document.querySelector(a.getAttribute('href'));
    if(!t) return; e.preventDefault();
    t.scrollIntoView({behavior:'smooth',block:'start'});
  });
});

})();
</script>"""

def _build_summary_html(data):
    import re as _re
    view = data.get('market_view', '')
    bc = 'badge-bull' if '多' in view else ('badge-bear' if '空' in view else 'badge-neutral')

    R_COL = {'強烈看多':'#c0392b','看多':'#b8860b','觀察中':'#2980b9','中性':'#7f8c8d','謹慎':'#884400'}
    R_LV  = {'強烈看多':5,'看多':4,'觀察中':3,'中性':2,'謹慎':1}
    def _dots(rating):
        lv = R_LV.get(rating, 0); col = R_COL.get(rating, '#ccc')
        return ''.join(f'<span style="display:inline-block;width:6px;height:6px;border-radius:1px;'
                       f'background:{col if i < lv else "#dde2ea"};margin:0 1px"></span>' for i in range(5))

    signals = ''.join(f'<li>{s}</li>' for s in data['signals']) or '<li>—</li>'

    # Stocks with conviction bars
    sr = ''.join(
        f'<tr><td class="code">[{s["code"]}]</td><td>{s["name"]}</td>'
        f'<td style="color:{R_COL.get(s["rating"],"#888")};font-weight:700;white-space:nowrap">'
        f'{s["rating"]}&nbsp;<span style="display:inline-flex;gap:1px;vertical-align:middle">{_dots(s["rating"])}</span></td></tr>'
        for s in data['stocks']
    )
    stocks = (f'<table class="summary-table"><thead><tr><th>代號</th><th>名稱</th><th>評級</th></tr></thead><tbody>{sr}</tbody></table>') if sr else '<p style="color:#999;font-size:.85em">—</p>'

    # Holdings as bar chart
    hc = ''
    for r in data['core_holdings']:
        if not r or not r[0]: continue
        name = r[0]; theme = r[1] if len(r) > 1 else ''; pct_s = r[2] if len(r) > 2 else ''
        m = _re.search(r'(\d+(?:\.\d+)?)', pct_s)
        pct_n = float(m.group(1)) if m else 0
        hc += (f'<div class="shc-row"><span class="shc-name" title="{name}">{name}</span>'
               f'<div class="shc-track"><div class="shc-fill" style="width:{min(pct_n,100):.0f}%"></div>'
               f'<span class="shc-pct">{pct_s}</span></div></div>')
    holdings = f'<div>{hc}</div>' if hc else '<p style="color:#999;font-size:.85em">—</p>'

    risks = ''.join(f'<li>{r}</li>' for r in data['risks']) or '<li>—</li>'
    badge = f'<span class="badge {bc}">大盤：{view}</span>' if view else ''
    return f"""<div class="summary-card">
  <div class="summary-header"><span class="summary-title">本集快覽</span>{badge}</div>
  <div class="summary-grid">
    <div class="summary-section signals-section"><h4>三大核心訊號</h4><ol>{signals}</ol></div>
    <div class="summary-section stocks-section"><h4>點名個股</h4>{stocks}</div>
    <div class="summary-section holdings-section"><h4>核心持股建議</h4>{holdings}</div>
    <div class="summary-section risks-section"><h4>主要風險</h4><ul>{risks}</ul></div>
  </div>
</div>"""

def convert_to_html(md_content, ep_title, date_str, output_path):
    try:
        import markdown as md_lib
        cleaned = _clean_text(md_content)
        summary_html = _build_summary_html(_extract_summary_data(cleaned))
        body_html = md_lib.Markdown(extensions=['tables', 'nl2br']).convert(cleaned)
        ep_short = ep_title if len(ep_title) < 40 else ep_title[:37] + '...'
        html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股癌研報 · {ep_short} · {date_str}</title>
<style>{HTML_CSS}</style>
</head>
<body>
<div class="cover"><div class="cover-inner">
  <h1>股癌 Podcast</h1>
  <div class="cover-subtitle">AI 深度投資研報</div>
  <div class="cover-ep">{ep_short}</div>
  <div class="cover-date">{date_str}</div>
  <div class="cover-disclaimer">本報告由人工智慧模型輔助生成，內容僅供參考，不構成任何投資建議或邀約。<br>投資有風險，入市前請獨立評估，自行承擔決策責任。</div>
</div></div>
<nav class="toc-mob"><ul id="toc-m"></ul></nav>
<div class="page-outer"><div class="page-flex">
  <aside class="toc-col">
    <div class="toc-box">
      <div class="toc-ttl">目錄</div>
      <ul class="toc-list" id="toc-d"></ul>
    </div>
  </aside>
  <div class="main-col">
    <div class="container">
{summary_html}
<div class="report-content" id="report">{body_html}</div>
    </div>
  </div>
</div></div>
<div class="page-footer">本報告基於 {ep_title}，分析日期：{date_str} · 由 AI 模型輔助生成 · 僅供參考，不構成投資建議</div>
{JS_BLOCK}
</body>
</html>"""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"✅ HTML 生成完成（{os.path.getsize(output_path)//1024} KB）: {output_path}")
        return True
    except Exception as e:
        print(f"❌ HTML 生成失敗: {e}")
        import traceback; traceback.print_exc()
        return False

def push_html_to_pages(html_path):
    """Commit and push the HTML report to the repo for GitHub Pages."""
    try:
        for cmd in [
            ['git', 'config', 'user.name', 'github-actions[bot]'],
            ['git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'],
            ['git', 'add', html_path],
            ['git', 'commit', '-m', f'report: add {os.path.basename(html_path)}'],
            ['git', 'push'],
        ]:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 and r.stderr.strip():
                print(f"  git: {r.stderr.strip()}")
        print("✅ 已推送到 GitHub Pages")
        return True
    except Exception as e:
        print(f"❌ Git push 失敗: {e}")
        return False


# ─── 寄送 Email ──────────────────────────────────────────────────────────────

def send_email(subject, body_html, config):
    recipients = config.get("recipients", [config["gmail_user"]])
    print(f"📧 正在寄送報告至 {recipients}...")
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = config['gmail_user']
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(config['gmail_user'], config['gmail_app_password'])
            server.send_message(msg)
        print("✅ 郵件寄送成功！")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ Gmail 認證失敗，請確認應用程式密碼是否正確。")
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

    # 7. 存 Markdown（備份）
    ts = time.strftime('%Y%m%d_%H%M')
    md_path = f"{REPORT_DIR}/研報_{ts}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(final_output)
    print(f"📄 Markdown 存檔：{md_path}")

    # 8. 轉 HTML
    date_str = time.strftime('%Y-%m-%d')
    html_filename = f"report_{ts}.html"
    html_path = os.path.join(DOCS_DIR if is_cloud else REPORT_DIR, html_filename)
    html_ok = convert_to_html(final_output, ep['title'], date_str, html_path)

    # 9. 推送到 GitHub Pages（雲端）並寄 Email
    report_url = None
    if is_cloud and html_ok:
        if push_html_to_pages(html_path):
            report_url = f"{GITHUB_PAGES_BASE}/reports/{html_filename}"
            print(f"🌐 報告網址：{report_url}")

    if html_ok:
        subject = f"【股癌研報】{ep['title']} — {time.strftime('%Y/%m/%d')}"
        if report_url:
            body_html = f"""<div style="font-family:-apple-system,'Microsoft JhengHei',sans-serif;max-width:600px;margin:0 auto;">
<div style="background:#0d2137;color:white;padding:24px 28px;border-bottom:3px solid #c89b32;">
  <h2 style="margin:0;font-size:1.1em;">股癌 Podcast AI 投資研報</h2>
</div>
<div style="background:white;padding:24px 28px;border:1px solid #dde2ea;">
  <p style="color:#2c2c3e;margin-bottom:16px;">您好，本週股癌 Podcast 分析研報已自動產生。</p>
  <table style="width:100%;font-size:.9em;color:#444;margin-bottom:20px;">
    <tr><td style="padding:4px 0;color:#888;width:80px">集數</td><td>{ep['title']}</td></tr>
    <tr><td style="padding:4px 0;color:#888">分析日期</td><td>{date_str}</td></tr>
    <tr><td style="padding:4px 0;color:#888">模型</td><td>{model}</td></tr>
  </table>
  <div style="text-align:center;margin:24px 0;">
    <a href="{report_url}" style="background:#0d2137;color:white;padding:12px 28px;text-decoration:none;border-radius:4px;font-weight:700;font-size:1em;border-bottom:3px solid #c89b32;">點此開啟完整研報 →</a>
  </div>
  <p style="color:#aaa;font-size:.8em;text-align:center;">此郵件由自動排程系統發送。報告內容由 AI 輔助生成，僅供參考。</p>
</div></div>"""
        else:
            body_html = f"<p>股癌研報已生成，集數：{ep['title']}，請至 GitHub 查看。</p>"
        send_email(subject, body_html, config)
    else:
        print("⚠️  HTML 生成失敗，跳過寄送。")

    # 10. 記錄已處理（防止重複執行）
    save_last_episode(ep)

    # 11. 清理暫存音檔
    cleanup()

    print(f"\n🎉 全部完成！報告存於 {REPORT_DIR}/")

if __name__ == "__main__":
    run()
