import ctypes
import logging
import os
import re
import sys
import threading
import subprocess
import shutil
import tempfile
from tkinter import *
from tkinter import filedialog, messagebox
from tkinter.ttk import *
from typing import Any, Callable, Union

if sys.platform == 'win32':
    import winreg

import requests
import yt_dlp
from sanitize_filename import sanitize

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

try:
    import translators as ts
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False

frozen = getattr(sys, 'frozen', False)

# Set working directory to app directory so that CWD is never a system directory (e.g. C:\Windows\System32)
try:
    _app_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if not frozen else os.path.dirname(sys.executable)
    _win_dir = os.environ.get('SystemRoot', r'C:\Windows').lower()
    if os.path.exists(_app_dir) and not _app_dir.lower().startswith(_win_dir):
        os.chdir(_app_dir)
except Exception:
    pass

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.addLevelName(15, 'STATUS')
logger.setLevel(15)
download_queue = []
ongoing_task: bool = False
logger.info(f'YT-DLP GUI (yt-dlp {yt_dlp.version.__version__}) (Python {sys.version})')


# ============================================================
# FOLDER STRUCTURE: Windows Videos\YT-DLP GUI\
# ============================================================
def get_windows_videos_folder() -> str:
    """Get Windows Videos folder (shell:MyVideo)"""
    if sys.platform == 'win32':
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders')
            videos_path = winreg.QueryValueEx(key, 'My Video')[0]
            winreg.CloseKey(key)
            return videos_path
        except:
            pass
    return os.path.join(os.path.expanduser('~'), 'Videos')


APP_FOLDER_NAME = 'YT-DLP GUI'
BASE_DIR = os.path.join(get_windows_videos_folder(), APP_FOLDER_NAME)
DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
SUBTITLES_DIR = os.path.join(BASE_DIR, 'subtitles')
TEMP_DIR = os.path.join(BASE_DIR, 'temp')
MODELS_DIR = os.path.join(BASE_DIR, 'models')

# Create all directories
for d in [BASE_DIR, DOWNLOADS_DIR, PROCESSED_DIR, SUBTITLES_DIR, TEMP_DIR, MODELS_DIR]:
    os.makedirs(d, exist_ok=True)

# Ensure tempfile and environment point to safe user temp directory instead of system folders
tempfile.tempdir = TEMP_DIR
if not os.environ.get('TEMP') or 'system32' in os.environ.get('TEMP', '').lower():
    os.environ['TEMP'] = TEMP_DIR
if not os.environ.get('TMP') or 'system32' in os.environ.get('TMP', '').lower():
    os.environ['TMP'] = TEMP_DIR

# Whisper model cache (memory)
_whisper_model_cache = {}
_whisper_model_lock = threading.Lock()


def get_whisper_model(model_name: str = 'tiny'):
    """Get or load Whisper model (cached in memory)"""
    global _whisper_model_cache
    with _whisper_model_lock:
        if model_name not in _whisper_model_cache:
            logger.info(f'Loading Whisper model: {model_name} (first time, will be cached)...')
            _whisper_model_cache[model_name] = whisper.load_model(model_name, download_root=MODELS_DIR)
            logger.info(f'Whisper model {model_name} loaded and cached to {MODELS_DIR}.')
        else:
            logger.info(f'Using cached Whisper model: {model_name}')
        return _whisper_model_cache[model_name]


# Get absolute path to ffmpeg.exe (bundled with the app)
def get_ffmpeg_path() -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(__file__))
    ffmpeg_path = os.path.join(base_path, 'ffmpeg.exe')
    if os.path.exists(ffmpeg_path):
        return ffmpeg_path
    return 'ffmpeg'


FFMPEG_PATH = get_ffmpeg_path()


def get_res_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller"""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(__file__))
    if os.path.exists(os.path.join(base_path, relative_path)):
        return os.path.join(base_path, relative_path)
    else:
        raise FileNotFoundError(f'{os.path.join(base_path, relative_path)} is not found!')


ydl_base_opts: dict[str, Any] = {
    'paths': {'temp': TEMP_DIR, 'home': DOWNLOADS_DIR},
    'outtmpl': os.path.join(DOWNLOADS_DIR, 'TITLE-%(id)s.%(ext)s'),
    'restrictfilenames': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'geo-bypass': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'windowsfilenames': True,
    'overwrites': True,
    'cachedir': False,
    'age_limit': 100,
    'noplaylist': True,
    'live_from_start': True,
    'no-video-multistreams': True,
    'no-audio-multistreams': True,
    'check_formats': 'selected',
    'fixup': 'detect_or_warn',
    'extractor_args': {'youtube': {'skip': ['dash', 'hls']}, },
    'ffmpeg_location': FFMPEG_PATH,
}
percent_str_regex = re.compile(r'\d{1,3}\.\d{1,2}%')


def handle_private_video(e: object, url: str, ydl_opts: dict,
                          func: Callable[[str, dict, bool], Union[dict, bool]]) -> Union[dict, bool]:
    """Returns info if func is successful after login and False if not"""
    result = False

    def handle_choose():
        nonlocal result
        if v.get() == 0:
            return
        browser = [browsers[v.get() - 1]]
        ydl_opts['cookiesfrombrowser'] = browser
        popup.title('Logging in...')
        choose_button_var.set('Logging in...')
        choose_button.configure(state=DISABLED)
        popup.update()
        info = func(url, ydl_opts, True)
        if info:
            ydl_base_opts['cookiesfrombrowser'] = browser
            result = info
            messagebox.showinfo('Login', 'Login successful! Specified browser will be used until you close this app.')
        else:
            messagebox.showerror('Login', 'Login failed. The browser you chose does not have an account with access to this video.')
        popup.destroy()

    if str(e).endswith('Private video. Sign in if you\'ve been granted access to this video'):
        browsers = ['brave', 'chrome', 'chromium', 'edge', 'firefox', 'opera', 'safari', 'vivaldi']
        popup = Toplevel(takefocus=True)
        popup.title('Choose browser to fetch account cookies from')
        Label(popup, text='This video is private. Please choose the browser you use that is logged into your video platform.\nIf you are on Windows and use Chrome or Edge, please close your browser before choosing.').pack()
        v = IntVar(value=0)
        choose_button_var = StringVar(value='Choose')
        for i, option in enumerate(browsers):
            Radiobutton(popup, text=option, variable=v, value=i + 1).pack(anchor="w")
        choose_button = Button(popup, command=handle_choose, textvariable=choose_button_var)
        choose_button.pack()
        root.wait_window(popup)
    return result


def download(urls: Union[list, str], ydl_opts=None, ignore_error: bool = False) -> bool:
    """Return whether download is successful"""
    if ydl_opts is None:
        ydl_opts = ydl_base_opts.copy()
    if 'paths' not in ydl_opts:
        ydl_opts['paths'] = {}
    if 'temp' not in ydl_opts['paths']:
        ydl_opts['paths']['temp'] = TEMP_DIR
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(urls if isinstance(urls, list) else [urls])
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as e:
        if ignore_error:
            return False
        return handle_private_video(e, urls, ydl_opts, download)
    except Exception as e:
        messagebox.showerror('Error', f'Error while downloading: {e}')
        return False
    else:
        return True


def extract_info(url: str, ydl_opts: dict, ignore_error: bool = False) -> dict:
    if not isinstance(ydl_opts, dict):
        ydl_opts = ydl_base_opts.copy()
    if 'paths' not in ydl_opts:
        ydl_opts['paths'] = {}
    if 'temp' not in ydl_opts['paths']:
        ydl_opts['paths']['temp'] = TEMP_DIR
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as e:
        if ignore_error:
            return {}
        result = handle_private_video(e, url, ydl_opts, extract_info)
        if not result:
            return {}
        else:
            return result
    except Exception as e:
        messagebox.showerror('Error', f'Error while extracting info: {e}')
        return {}


# ============================================================
# UTILITY FUNCTIONS
# ============================================================
def format_time(seconds: float) -> str:
    """Convert seconds to SRT time format HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def write_srt(segments: list, filepath: str):
    """Write whisper segments to SRT file"""
    with open(filepath, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, 1):
            f.write(f'{i}\n')
            f.write(f"{format_time(seg['start'])} --> {format_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")


def embed_subtitles_ffmpeg(video_path: str, srt_path: str, output_path: str) -> bool:
    """Embed SRT subtitles into video using ffmpeg (soft-sub, mov_text)"""
    try:
        cmd = [
            FFMPEG_PATH, '-y',
            '-i', video_path,
            '-i', srt_path,
            '-c', 'copy',
            '-c:s', 'mov_text',
            '-map', '0:v', '-map', '0:a', '-map', '1',
            '-metadata:s:s:0', 'language=tur',
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        logger.error(f'FFmpeg embed error: {e}')
        return False


def find_downloaded_file(title: str, download_dir: str) -> str:
    """Find the downloaded video file in the download directory"""
    import glob
    safe_title = sanitize(title)
    pattern = os.path.join(download_dir, f'{safe_title}*')
    files = glob.glob(pattern)
    video_exts = ('.mp4', '.webm', '.mkv', '.m4v', '.mov', '.avi')
    for f in files:
        if f.lower().endswith(video_exts) and not ('_subbed' in f or '_turkish' in f):
            return f
    return ''


class ScrolledWindow(Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.parent = parent
        self.xscrlbr = Scrollbar(self.parent, orient=HORIZONTAL)
        self.xscrlbr.pack(fill=X, side=BOTTOM)
        self.yscrlbr = Scrollbar(self.parent)
        self.yscrlbr.pack(fill=Y, side=RIGHT)
        self.canv = Canvas(self.parent)
        self.canv.config(relief='flat', width=10, height=10, bd=2)
        self.canv.pack(side=TOP, fill=BOTH, expand=True)
        self.xscrlbr.config(command=self.canv.xview)
        self.yscrlbr.config(command=self.canv.yview)
        self.scrollwindow = Frame(self.parent)
        self.canv.create_window(0, 0, window=self.scrollwindow, anchor=NW)
        self.canv.config(xscrollcommand=self.xscrlbr.set, yscrollcommand=self.yscrlbr.set, scrollregion=self.canv.bbox('all'))
        self.yscrlbr.lift(self.scrollwindow)
        self.xscrlbr.lift(self.scrollwindow)
        self.scrollwindow.bind('<Configure>', self._configure_window)
        self.scrollwindow.bind('<Enter>', self._bound_to_mousewheel)
        self.scrollwindow.bind('<Leave>', self._unbound_to_mousewheel)

    def _bound_to_mousewheel(self, event):
        self.canv.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbound_to_mousewheel(self, event):
        self.canv.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self.canv.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _configure_window(self, event):
        size = (self.scrollwindow.winfo_reqwidth(), self.scrollwindow.winfo_reqheight())
        self.canv.config(scrollregion='0 0 %s %s' % size)
        if self.scrollwindow.winfo_reqwidth() != self.canv.winfo_width():
            self.canv.config(width=self.scrollwindow.winfo_reqwidth())
        if self.scrollwindow.winfo_reqheight() != self.canv.winfo_height():
            self.canv.config(height=self.scrollwindow.winfo_reqheight())


try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    pass


# ============================================================
# DOWNLOAD TASKS
# ============================================================
class DownloadTask(Frame):
    def __init__(self, url: str, path: str, ydl_opts: dict, parent: Widget):
        super().__init__(parent, borderwidth=2, relief='groove')
        self.url = url
        self.parent = parent
        self.ydl_opts = ydl_opts
        self.thread = None
        self.ydl_opts['progress_hooks'] = [self.progress_hook]
        self.ydl_opts['postprocessor_hooks'] = [self.postprocessor_hook]
        self.status = StringVar(value='Queued')
        self.progress = IntVar(value=0)

        self.extract_info_succeed = False
        info = extract_info(url, ydl_opts)
        if not info:
            return
        parsed_info = parse_info(info)
        self.extract_info_succeed = True
        self.title = sanitize(parsed_info['title'])
        self.duration = parsed_info['duration']
        self.size = parsed_info['size']
        self.formats = parsed_info['formats']
        if isinstance(self.ydl_opts['outtmpl'], dict):
            self.ydl_opts['outtmpl']['default'] = self.ydl_opts['outtmpl']['default'].replace('TITLE', self.title)
        else:
            self.ydl_opts['outtmpl'] = self.ydl_opts['outtmpl'].replace('TITLE', self.title)

        Label(self, text=self.title).pack(side=TOP)
        details_frame = Frame(self)
        details_frame.pack(side=TOP)
        Label(details_frame, text=f'Duration: {self.duration}').pack(side=LEFT)
        Label(details_frame, text=f'Size: {round(self.size / (1024 * 1024), 2)}MB' if self.size else 'unknown size').pack(side=LEFT)
        format_str = ''
        if self.formats['video']:
            format_str += f'Video: {self.formats["video"]["resolution"]}@{self.formats["video"]["fps"]}fps {self.formats["video"]["codec"]} {"HDR" if self.formats["video"]["hdr"] else ""}'
        if self.formats['audio']:
            format_str += f'Audio: {self.formats["audio"]["sample_rate"]} {self.formats["audio"]["bitrate"]} {self.formats["audio"]["codec"]}'
        Label(details_frame, text=format_str).pack(side=LEFT)
        Label(self, text=f'Save to: {path}').pack(side=TOP)
        self.progress_bar = Progressbar(self, orient=HORIZONTAL, length=100, mode='determinate', variable=self.progress, value=0)
        self.progress_bar.pack(side=TOP, fill=X, expand=True, padx=10)
        Label(self, textvariable=self.status, anchor=CENTER).pack(side=TOP, fill=X, expand=True, padx=10)
        self.pack(side=TOP, fill=X, pady=(0, 5), ipadx=5)

    def start_task(self):
        global ongoing_task
        status('Downloading')
        self.thread = threading.Thread(target=download, args=(self.url, self.ydl_opts), daemon=True)
        self.thread.start()
        ongoing_task = True

    def progress_hook(self, d: dict):
        global ongoing_task
        if d['status'] == 'downloading' and '_default_template' in d:
            percent_str = percent_str_regex.search(d['_percent_str'])
            if percent_str:
                percent_str = percent_str.group()
                self.status.set(f'Downloading: {d["_default_template"]}')
                self.progress.set(int(float(percent_str.rstrip('%'))))
        elif d['status'] == 'finished':
            if self in download_queue:
                download_queue.remove(self)
            self.status.set('Finished')
            ongoing_task = False
            status('Ready', log=False)
        elif d['status'] == 'error':
            if self in download_queue:
                download_queue.remove(self)
            ongoing_task = False
            self.status.set('Download error')
            status('Ready', log=False)

    def postprocessor_hook(self, d: dict):
        global ongoing_task
        if d['status'] == 'started' or d['status'] == 'processing':
            self.status.set('Post-processing')
        else:
            if self in download_queue:
                download_queue.remove(self)
            self.status.set('Finished')
            status('Ready', log=False)


class AISubtitleTask(Frame):
    """Download task that also generates AI subtitles (Turkish) and embeds them"""
    def __init__(self, url: str, path: str, ydl_opts: dict, parent: Widget):
        super().__init__(parent, borderwidth=2, relief='groove')
        self.url = url
        self.path = path
        self.ydl_opts = ydl_opts
        self.parent = parent
        self.thread = None
        self.status = StringVar(value='Queued')
        self.progress = IntVar(value=0)

        self.extract_info_succeed = False
        info = extract_info(url, ydl_opts)
        if not info:
            return
        parsed_info = parse_info(info)
        self.extract_info_succeed = True
        self.title = sanitize(parsed_info['title'])
        self.duration = parsed_info['duration']
        self.size = parsed_info['size']
        self.formats = parsed_info['formats']
        if isinstance(self.ydl_opts['outtmpl'], dict):
            self.ydl_opts['outtmpl']['default'] = self.ydl_opts['outtmpl']['default'].replace('TITLE', self.title)
        else:
            self.ydl_opts['outtmpl'] = self.ydl_opts['outtmpl'].replace('TITLE', self.title)

        Label(self, text=f'{self.title} [Türkçe AI Altyazı]').pack(side=TOP)
        details_frame = Frame(self)
        details_frame.pack(side=TOP)
        Label(details_frame, text=f'Duration: {self.duration}').pack(side=LEFT)
        Label(details_frame, text=f'Size: {round(self.size / (1024 * 1024), 2)}MB' if self.size else 'unknown size').pack(side=LEFT)
        format_str = ''
        if self.formats['video']:
            format_str += f'Video: {self.formats["video"]["resolution"]}@{self.formats["video"]["fps"]}fps {self.formats["video"]["codec"]} {"HDR" if self.formats["video"]["hdr"] else ""}'
        if self.formats['audio']:
            format_str += f'Audio: {self.formats["audio"]["sample_rate"]} {self.formats["audio"]["bitrate"]} {self.formats["audio"]["codec"]}'
        Label(details_frame, text=format_str).pack(side=LEFT)
        Label(self, text=f'Downloads: {DOWNLOADS_DIR}').pack(side=TOP)
        Label(self, text=f'Output: {PROCESSED_DIR}').pack(side=TOP)
        self.progress_bar = Progressbar(self, orient=HORIZONTAL, length=100, mode='determinate', variable=self.progress, value=0)
        self.progress_bar.pack(side=TOP, fill=X, expand=True, padx=10)
        Label(self, textvariable=self.status, anchor=CENTER).pack(side=TOP, fill=X, expand=True, padx=10)
        self.pack(side=TOP, fill=X, pady=(0, 5), ipadx=5)

    def start_task(self):
        global ongoing_task
        status('Downloading video')
        self.thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self.thread.start()
        ongoing_task = True

    def _run_pipeline(self):
        global ongoing_task
        try:
            # Step 1: Download video to DOWNLOADS_DIR
            self.status.set('Downloading video...')
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                ydl.download([self.url])

            # Find downloaded video file
            video_file = find_downloaded_file(self.title, self.path) or find_downloaded_file(self.title, DOWNLOADS_DIR)
            if not video_file or not os.path.exists(video_file):
                self.status.set('Error: Video file not found in downloads')
                ongoing_task = False
                return

            # Step 2: Transcribe with Whisper (cached model)
            self.status.set('Transcribing with AI (Whisper)...')
            self.progress.set(10)
            model = get_whisper_model('tiny')
            result = model.transcribe(video_file, fp16=False, word_timestamps=True, language=None)

            # Step 3: Translate to Turkish
            if TRANSLATOR_AVAILABLE:
                self.status.set('Translating to Turkish...')
                self.progress.set(30)
                translated_segments = []
                for i, seg in enumerate(result['segments']):
                    try:
                        translated_text = ts.translate_text(seg['text'], translator='google', from_language='auto', to_language='tr')
                    except Exception as e:
                        logger.warning(f'Translation failed for segment {i}: {e}')
                        translated_text = seg['text']
                    translated_segments.append({
                        'start': seg['start'],
                        'end': seg['end'],
                        'text': translated_text
                    })
                segments_to_write = translated_segments
            else:
                self.status.set('Translator not available, using original language...')
                segments_to_write = result['segments']

            # Step 4: Write SRT to SUBTITLES_DIR
            self.status.set('Generating Turkish subtitle file...')
            self.progress.set(50)
            srt_filename = f'{self.title}.srt'
            srt_file = os.path.join(SUBTITLES_DIR, srt_filename)
            write_srt(segments_to_write, srt_file)

            # Step 5: Embed subtitles with ffmpeg -> output to PROCESSED_DIR
            self.status.set('Embedding subtitles into video...')
            self.progress.set(70)
            output_filename = f'{self.title}_turkish.mp4'
            output_file = os.path.join(PROCESSED_DIR, output_filename)
            success = embed_subtitles_ffmpeg(video_file, srt_file, output_file)

            if success:
                self.status.set('Finished (Turkish AI subtitles embedded)')
                self.progress.set(100)
                logger.info(f'Success: {output_file}')
                logger.info(f'Subtitle file: {srt_file}')
                logger.info(f'Original video kept in: {video_file}')
            else:
                self.status.set('Error: Failed to embed subtitles')

        except Exception as e:
            self.status.set(f'Error: {e}')
            logger.error(f'AI Subtitle pipeline error: {e}')
        finally:
            if self in download_queue:
                download_queue.remove(self)
            ongoing_task = False
            status('Ready', log=False)


# ============================================================
# GUI SETUP
# ============================================================
root = Tk()
root.iconphoto(True, PhotoImage(file=get_res_path('icon.png')))
root.title('YT-DLP GUI - Initializing')
status_var = StringVar(value='Initializing')
status_bar = LabelFrame(root, padding=(2, 2))
status_bar.pack(expand=True, fill=X, side=BOTTOM, pady=(0, 0))
status_bar.rowconfigure(0, weight=1)
status_bar.columnconfigure(1, weight=1)
status_text = Label(status_bar, textvariable=status_var, anchor=E)
status_text.grid(row=0, column=2, sticky=E)


def status(text: Any, log: bool = True):
    global status_text
    text = text.strip()
    status_var.set(text)
    text = text.replace('\n', ' ')
    if log:
        if text.lower() == 'ready':
            logger.log(15, text + '\n')
        else:
            logger.log(15, text)
    root.title(f'YT-DLP GUI - {text}')


def parse_info(info: dict, best_format_only: bool = True) -> dict:
    title = info['title']
    duration = info['duration_string']
    size = info.get('filesize', info.get('filesize_approx', 0))
    subtitles = info['subtitles']
    if best_format_only:
        if 'requested_formats' in info:
            temp = [parse_format(f) for f in info['requested_formats']]
            formats = {}
            for f in temp:
                if f['video']:
                    formats['video'] = f['video']
                if f['audio']:
                    formats['audio'] = f['audio']
        else:
            format_id = info['format_id']
            for f in info['formats']:
                if f['format_id'] == format_id:
                    formats = parse_format(f)
                    break
    else:
        formats = [parse_format(f) for f in info['formats']
                   if f.get('format_note', '') != 'storyboard'
                   and (f.get('vcodec', 'none') != 'none' or f.get('acodec', 'none') != 'none')
                   and f.get('url', '')]
    return {'title': title, 'duration': duration, 'size': size, 'subtitles': subtitles, 'formats': formats}


def parse_format(format: dict) -> dict:
    parsed = {'video': {}, 'audio': {}}
    contains_video = format.get('vcodec', 'none') != 'none'
    contains_audio = format.get('acodec', 'none') != 'none'
    parsed['format_id'] = format['format_id']
    parsed['ext'] = format['ext']
    parsed['size'] = format.get('filesize', format.get('filesize_approx', 0))

    def parse_codec(codec: str) -> str:
        mapping = {'mp4v': 'H263', 'av01': 'AV1', 'avc1': 'H264/AVC', 'hev1': 'H265/HEVC',
                   'vp9': 'VP9', 'vp09': 'VP9', 'vp8': 'VP8', 'mp4a': 'AAC', 'opus': 'Opus'}
        return mapping.get(codec.split('.')[0].lower(), codec.split('.')[0].lower())

    if contains_video:
        parsed['video'] = {'resolution': format.get('resolution', 'unknown'), 'fps': format.get('fps', 0),
                           'codec': parse_codec(format.get('vcodec', 'none')),
                           'hdr': format.get('dynamic_range', 'SDR') != 'SDR'}
    if contains_audio:
        parsed['audio'] = {'sample_rate': f'{round(format.get("asr", 0) / 1000, 2)}khz' if format.get('asr') else 'unknown sample rate',
                           'bitrate': f'{round(format.get("abr", 0))}kbps' if format.get('abr', None) and float(format.get('abr', 0)) else 'unknown bitrate',
                           'codec': parse_codec(format.get('acodec', 'none'))}
    return parsed


def check_url(url: str) -> bool:
    try:
        requests.get(url, timeout=5)
    except:
        return False
    return True


def handle_download_video_best(url: str, path: str):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    ydl_opts = ydl_base_opts.copy()
    ydl_opts['paths'] = {'home': path, 'temp': TEMP_DIR}
    ydl_opts['outtmpl'] = os.path.join(path, os.path.basename(ydl_opts['outtmpl']))
    download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))


def handle_download_audio_best(url: str, path: str):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    ydl_opts = ydl_base_opts.copy()
    ydl_opts.update({'format': 'bestaudio'})
    ydl_opts['paths'] = {'home': path, 'temp': TEMP_DIR}
    ydl_opts['outtmpl'] = os.path.join(path, os.path.basename(ydl_opts['outtmpl']).replace('.%(ext)s', '_audio.%(ext)s'))
    download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))


def handle_download_video_ai_subs(url: str, path: str):
    """Download video + generate Turkish AI subtitles + embed"""
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    if not WHISPER_AVAILABLE:
        messagebox.showerror('Error', 'openai-whisper not installed. Run: pip install openai-whisper')
        return
    if not TRANSLATOR_AVAILABLE:
        messagebox.showwarning('Warning', 'translators module not installed. Subtitles will be in original language.\nRun: pip install translators')

    ydl_opts = ydl_base_opts.copy()
    ydl_opts['paths'] = {'home': path, 'temp': TEMP_DIR}
    ydl_opts['outtmpl'] = os.path.join(path, os.path.basename(ydl_opts['outtmpl']))
    download_queue.append(AISubtitleTask(url, path, ydl_opts, queue_frame))


def handle_download_info(url: str, path: str, ydl_opts: dict = None):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    if not ydl_opts:
        ydl_opts = ydl_base_opts.copy()
    if 'paths' not in ydl_opts:
        ydl_opts['paths'] = {}
    ydl_opts['paths']['home'] = path
    ydl_opts['paths']['temp'] = TEMP_DIR
    status('Extracting info')
    info = extract_info(url, ydl_opts)
    if not info:
        status('Ready')
        return
    parsed_info = parse_info(info, best_format_only=False)
    details_window = Toplevel(takefocus=True)
    details_window.title('Extracted Info')
    Label(details_window, text=f'{parsed_info["title"]} ({parsed_info["duration"]})', anchor=CENTER).pack(side=TOP, fill=X, expand=True, padx=10)

    scroll_container_frame = Frame(details_window)
    scroll_container_frame.pack(expand=True, fill=BOTH, side=TOP)
    scrollableFrame = ScrolledWindow(scroll_container_frame)
    formats_frame = LabelFrame(scrollableFrame.scrollwindow, text='Download Options')
    formats_frame.pack(side=TOP, fill=BOTH, expand=True, padx=(10, 10), pady=(10, 0))
    formats = parsed_info['formats']

    def on_select_single_format(*args):
        if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is None:
            for radio_button in radiobutton_frame.winfo_children():
                radio_button.configure(state=NORMAL)
            selected_format.set('')
        else:
            for radio_button in radiobutton_frame.winfo_children():
                radio_button.configure(state=DISABLED)
            selected = []
            if video_only_formats[video_selected_format.get()]:
                selected.append(video_only_formats[video_selected_format.get()])
            if audio_only_formats[audio_selected_format.get()]:
                selected.append(audio_only_formats[audio_selected_format.get()])
            selected_format.set('+'.join(selected))
        try:
            if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is not None:
                audio_convert_selector.configure(state=NORMAL)
            else:
                audio_convert_selector.configure(state=DISABLED)
                audio_convert_quality_selector.configure(state=DISABLED)
        except NameError:
            pass

    def on_select_format(*args):
        download_button.configure(state=NORMAL if selected_format.get() else DISABLED)

    def on_select_audio_convert_format(*args):
        audio_convert_quality_selector.configure(state=NORMAL if audio_convert_format.get() != 'Do not convert' else DISABLED)

    def handle_download():
        nonlocal ydl_opts
        ydl_opts['format'] = selected_format.get()
        if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is not None and valid_audio_convert_formats[audio_convert_format.get()] is not None:
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': valid_audio_convert_formats[audio_convert_format.get()], 'preferredquality': audio_convert_quality_values[audio_convert_quality.get()]}]
        ydl_opts['paths'] = {'home': path, 'temp': TEMP_DIR}
        ydl_opts['outtmpl'] = os.path.join(path, ydl_opts['outtmpl'] if isinstance(ydl_opts['outtmpl'], str) else ydl_opts['outtmpl']['default'])
        download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))
        ydl_opts = ydl_base_opts.copy()
        details_window.destroy()
        root.lift()

    download_button = Button(scrollableFrame.scrollwindow, text='Download', command=handle_download)
    selected_format = StringVar()
    selected_format.trace('w', on_select_format)
    video_only_formats, audio_only_formats = {'Select video format': None}, {'Select audio format': None}
    radiobutton_frame = Frame(formats_frame, relief='groove', borderwidth=3)
    radiobutton_frame.pack(side=TOP, fill=X, expand=True)
    for f in formats:
        if f['video'] and f['audio']:
            Radiobutton(radiobutton_frame, variable=selected_format, value=f['format_id'],
                        text=f'Video: {f["video"]["resolution"]}@{f["video"]["fps"]}fps {f["video"]["codec"]} {"HDR" if f["video"]["hdr"] else ""}\n'
                             f'Audio: {f["audio"]["sample_rate"]} {f["audio"]["bitrate"]} {f["audio"]["codec"]}\n'
                             f'File extension: {f["ext"]} Size: ' + (f'{round(f["size"] / (1024 * 1024), 2)}MB' if f["size"] else 'unknown size')).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
        else:
            if f['video']:
                video_only_formats[f'{f["video"]["resolution"]}@{f["video"]["fps"]}fps {f["video"]["codec"]} {"HDR" if f["video"]["hdr"] else ""}'] = f['format_id']
            if f['audio']:
                audio_only_formats[f'{f["audio"]["sample_rate"]} {f["audio"]["bitrate"]} {f["audio"]["codec"]}'] = f['format_id']
    if not len(radiobutton_frame.winfo_children()):
        Label(radiobutton_frame, text='No stream containing both video and audio available', anchor=CENTER).pack(side=TOP, fill=X, expand=True)

    custom_formats_frame = LabelFrame(formats_frame, text='Customize', borderwidth=3)
    custom_formats_frame.pack(side=TOP, fill=X, expand=True)
    video_selected_format = StringVar(value='Select video format')
    video_selected_format.trace('w', on_select_single_format)
    audio_selected_format = StringVar(value='Select audio format')
    audio_selected_format.trace('w', on_select_single_format)
    OptionMenu(custom_formats_frame, video_selected_format, 'Select video format', *video_only_formats.keys()).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
    OptionMenu(custom_formats_frame, audio_selected_format, 'Select audio format', *audio_only_formats.keys()).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
    audio_convert_format = StringVar(value='Do not convert')
    valid_audio_convert_formats = {'Do not convert': None, 'AAC (.m4a)': 'aac', 'ALAC (.m4a)': 'alac', 'FLAC (.flac)': 'flac', 'm4a (.m4a)': 'm4a', 'mp3 (.mp3)': 'mp3', 'Opus (.opus)': 'opus', 'Vorbis (.ogg)': 'vorbis', 'WAV (.wav)': 'wav'}
    audio_convert_frame = LabelFrame(formats_frame, text='Convert audio format to', borderwidth=3)
    audio_convert_frame.pack(side=TOP, fill=X, expand=True)
    audio_convert_selector = OptionMenu(audio_convert_frame, audio_convert_format, 'Do not convert', *valid_audio_convert_formats.keys(), command=on_select_audio_convert_format)
    audio_convert_selector.pack(side=TOP, fill=X, expand=True, pady=(5, 5))
    audio_convert_selector.configure(state=DISABLED)
    audio_convert_quality = StringVar(value='5')
    audio_convert_quality_values = {k: v for v, k in enumerate(['0 (Highest Quality)', '1', '2', '3', '4', '5 (Medium Quality)', '6', '7', '8', '9', '10 (Lowest Quality)'])}
    audio_convert_quality_selector = OptionMenu(audio_convert_frame, audio_convert_quality, '5 (Medium Quality)', *audio_convert_quality_values)
    audio_convert_quality_selector.pack(side=TOP, fill=X, expand=True, pady=(5, 5))
    audio_convert_quality_selector.configure(state=DISABLED)
    download_button.pack(side=TOP, fill=X, expand=True, padx=10, pady=(0, 10))
    status('Ready')


def select_save_path():
    path = filedialog.askdirectory(initialdir=DOWNLOADS_DIR, title='Choose a folder to save the downloaded file')
    if path:
        path_input.delete(0, END)
        path_input.insert(0, path)
    if sys.platform == 'win32':
        winreg.SetValue(winreg.HKEY_CURRENT_USER, 'Software\\YT-DLP GUI', winreg.REG_SZ, path)
    else:
        os.makedirs('~/.config/yt-dlp', exist_ok=True)
        with open('~/.config/yt-dlp/last_path.txt', 'w+') as f:
            f.write(path)


def open_folder(path: str):
    """Open folder in Windows Explorer"""
    if sys.platform == 'win32':
        os.startfile(path)
    else:
        subprocess.run(['xdg-open', path])


def do_tasks():
    if download_queue and not ongoing_task:
        task = download_queue[0]
        if task.extract_info_succeed:
            task.start_task()
        else:
            download_queue.remove(task)
            status('Ready')
    queue_frame.after(1000, do_tasks)
    root.update()


# Initial directory - use our organized downloads folder
initial_dir = DOWNLOADS_DIR
if sys.platform == 'win32':
    try:
        initial_dir = winreg.QueryValue(winreg.HKEY_CURRENT_USER, 'Software\\YT-DLP GUI')
    except FileNotFoundError:
        pass


# ============================================================
# UI LAYOUT
# ============================================================
# URL Input
url_input_frame = Frame(root)
url_input_frame.pack(expand=True, fill=BOTH, side=TOP, pady=(5, 10))
Label(url_input_frame, text='URL: ').pack(side=LEFT, padx=(10, 0))
url_input = Entry(url_input_frame, width=50)
url_input.pack(expand=True, fill=X, side=LEFT, padx=(2, 10))
url_input.focus_set()

# Path Input (defaults to organized downloads folder)
path_input_frame = Frame(root)
path_input_frame.pack(expand=True, fill=BOTH, side=TOP, pady=(5, 10))
Label(path_input_frame, text='Download Folder: ').pack(side=LEFT, padx=(10, 0))
path_input = Entry(path_input_frame, width=50)
path_input.pack(expand=True, fill=X, side=LEFT, padx=(2, 0))
path_input.insert(0, DOWNLOADS_DIR.replace('\\', '/'))
path_select_button = Button(path_input_frame, text='Choose', command=select_save_path)
path_select_button.pack(side=LEFT, padx=(2, 10))

# Buttons
buttons_frame = Frame(root)
buttons_frame.pack(expand=True, fill=BOTH, side=TOP, anchor=NW, padx=10, pady=5)

download_video_button = Button(buttons_frame, text='Download Video', command=lambda: handle_download_video_best(url_input.get(), path_input.get()))
download_video_button.pack(expand=True, fill=X, side=LEFT, padx=(0, 5))

download_audio_button = Button(buttons_frame, text='Download Audio', command=lambda: handle_download_audio_best(url_input.get(), path_input.get()))
download_audio_button.pack(expand=True, fill=X, side=LEFT, padx=5)

download_ai_subs_button = Button(buttons_frame, text='Video + Türkçe AI Altyazı', command=lambda: handle_download_video_ai_subs(url_input.get(), path_input.get()))
download_ai_subs_button.pack(expand=True, fill=X, side=LEFT, padx=5)

download_info_button = Button(buttons_frame, text='Customize Downloads', command=lambda: handle_download_info(url_input.get(), path_input.get()))
download_info_button.pack(expand=True, fill=X, side=LEFT, padx=5)

# Folder shortcut buttons
folder_buttons_frame = Frame(root)
folder_buttons_frame.pack(expand=True, fill=BOTH, side=TOP, anchor=NW, padx=10, pady=(0, 5))

Button(folder_buttons_frame, text='📁 Open Downloads', command=lambda: open_folder(DOWNLOADS_DIR)).pack(side=LEFT, padx=5)
Button(folder_buttons_frame, text='📁 Open Processed', command=lambda: open_folder(PROCESSED_DIR)).pack(side=LEFT, padx=5)
Button(folder_buttons_frame, text='📁 Open Subtitles', command=lambda: open_folder(SUBTITLES_DIR)).pack(side=LEFT, padx=5)
Button(folder_buttons_frame, text='📁 Open Base Folder', command=lambda: open_folder(BASE_DIR)).pack(side=LEFT, padx=5)

# Queue
scroll_container_frame = Frame(root)
scroll_container_frame.pack(expand=True, fill=BOTH, side=TOP, padx=10, pady=(0, 10))
scrollableFrame = ScrolledWindow(scroll_container_frame)
queue_frame = LabelFrame(scrollableFrame.scrollwindow, text='Download Queue')
queue_frame.pack(fill=BOTH, expand=True, anchor=CENTER, padx=(10, 10), pady=(10, 0))

do_tasks()

status('Ready')
root.mainloop()