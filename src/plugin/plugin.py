import json
import re
from os import chmod, remove, system
from os.path import basename, exists
import threading
from queue import Queue

from enigma import eTimer, fbClass, eRCInput, getDesktop, eDVBVolumecontrol

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.AVSwitch import avSwitch
from Components.config import config, ConfigSubsection, ConfigYesNo
from Components.ActionMap import HelpableActionMap
from Components.Console import Console
from Components.PluginComponent import PluginDescriptor

from Components.ServiceEventTracker import InfoBarBase
from Components.ServiceEventTracker import ServiceEventTracker
from Components.Sources.StaticText import StaticText
from Components.SystemInfo import BoxInfo

from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from Screens.Standby import QUIT_KODI, TryQuitMainloop

from Screens.InfoBarGenerics import InfoBarNotifications, InfoBarSeek, InfoBarAudioSelection, InfoBarShowHide, InfoBarSubtitleSupport
from Tools.BoundFunction import boundFunction
from Tools.Directories import fileWriteLine
from Tools import Notifications

from .e2utils import InfoBarAspectChange, WebPixmap, MyAudioSelection, \
    StatusScreen, getPlayPositionInSeconds, getDurationInSeconds, \
    InfoBarSubservicesSupport
from enigma import eServiceReference, eTimer, ePythonMessagePump, \
    iPlayableService, fbClass, eRCInput, getDesktop, eDVBVolumecontrol
from .server import KodiExtRequestHandler, UDSServer

config.kodi = ConfigSubsection()
config.kodi.addToMainMenu = ConfigYesNo(False)
config.kodi.addToExtensionMenu = ConfigYesNo(True)
config.kodi.standalone = ConfigYesNo(False)

MACHINEBRAND = BoxInfo.getItem("displaybrand")

try:
    from Plugins.Extensions.SubsSupport import SubsSupport, SubsSupportStatus
except ImportError:
    class SubsSupport(object):
        def __init__(self, *args, **kwargs):
            pass

    class SubsSupportStatus(object):
        def __init__(self, *args, **kwargs):
            pass

(OP_CODE_EXIT,
OP_CODE_PLAY,
OP_CODE_PLAY_STATUS,
OP_CODE_PLAY_STOP,
OP_CODE_SWITCH_TO_ENIGMA2,
OP_CODE_SWITCH_TO_KODI) = range(6)
KODIRUN_SCRIPT = "unset PYTHONPATH;kodi;kodiext -T"
KODIRESUME_SCRIPT = "kodiext -P %s -K"
KODIEXT_SOCKET = "/tmp/kodiext.socket"
KODIEXTIN = "/tmp/kodiextin.json"
KODI_LAUNCHER = None

SESSION = None
SERVER = None
SERVER_THREAD = None

# Path to Enigma2 settings file
settings_file = "/etc/enigma2/settings"

# Example resolution_map array
resolution_map = {
    "": (1280, 720),  # Default resolution for empty videomode
    "1080p": (1920, 1080),
    "1080i": (1920, 1080),
    "2160p": (3840, 2160),
    "2160p30": (3840, 2160)
}

# Initialize video_mode with a default value (empty string)
video_mode = ""

# Simulate reading the configuration file
try:
    with open(settings_file, "r") as file:
        for line in file:
            match = re.search(r"config\.av\.videomode\.HDMI=(\d+p)", line)
            if match:
                video_mode = match.group(1)  # Extract resolution key like "2160p"
                break
except FileNotFoundError:
    print(f"Arquivo de configuração não encontrado: {settings_file}")
    video_mode = ""  # Define um valor padrão

# Set resolution based on video_mode
if video_mode in resolution_map:
    _g_dw, _g_dh = resolution_map[video_mode]
else:
    # If video_mode is not found in resolution_map, use the default resolution
    _g_dw, _g_dh = resolution_map[""]

print(f"Video Mode: {video_mode}")
print(f"Resolution: {_g_dw}x{_g_dh}")

class SetAudio:
    def __init__(self):
        self.VolPrev = 0
        self.VolPlayer = 0
        self.volctrl = eDVBVolumecontrol.getInstance()
        self.ac3 = "downmix"
        self.dts = "downmix"
        self.aac = "passthrough"
        self.aacplus = "passthrough"

    def switch(self, Tokodi=False, Player=False):
        """Switch beetween audio profiles, assuring the volume is passed correctly."""
        current_vol = self.volctrl.getVolume()

        if Tokodi:
            if Player:
                self.VolPlayer = current_vol  # Save volume level before do E2Player start
            else:
                self.VolPrev = current_vol  # Save volume level before chaneg to Kodi
            vol = 50  # Define static volume level on Kodi
            ac3, dts, aac, aacplus = "downmix", "downmix", "passthrough", "passthrough"
        else:
            vol = self.VolPlayer if Player else self.VolPrev  # Restore correct volume level
            ac3, dts, aac, aacplus = self.ac3, self.dts, self.aac, self.aacplus

        self.volctrl.setVolume(vol, vol)  # Set the volume at
        self._apply_audio_settings(ac3, dts, aac, aacplus)

    def ReadData(self):
        """Read audio configurations befeor switching."""
        self.VolPrev = self.volctrl.getVolume()
        self.VolPlayer = self.VolPrev  # Assure that  E2Player herritage the correct volume level
        self.ac3 = self._read_audio_option("/proc/stb/audio/ac3", "CanDownmixAC3")
        self.dts = self._read_audio_option("/proc/stb/audio/dts", "CanDownmixDTS")
        self.aac = self._read_audio_option("/proc/stb/audio/aac", "CanDownmixAAC")
        self.aacplus = self._read_audio_option("/proc/stb/audio/aacplus", "CanDownmixAACPlus")

    def _apply_audio_settings(self, ac3, dts, aac, aacplus):
        """Aplly audio configurations checking sistem compatibility."""
        self._write_audio_option("/proc/stb/audio/ac3", ac3, "CanDownmixAC3")
        self._write_audio_option("/proc/stb/audio/dts", dts, "CanDownmixDTS")
        self._write_audio_option("/proc/stb/audio/aac", aac, "CanDownmixAAC")
        self._write_audio_option("/proc/stb/audio/aacplus", aacplus, "CanDownmixAACPlus")

    def _write_audio_option(self, path, value, system_key):
        """Write in the system, the audio configuration, if suported."""
        if BoxInfo.getItem(system_key):
            try:
                with open(path, "w") as f:
                    f.write(value)
            except IOError:
                print(f"Error writing in {path}")

    def _read_audio_option(self, path, system_key):
        """Read system audio configuration, if suported."""
        if BoxInfo.getItem(system_key):
            try:
                with open(path, "r") as f:
                    return f.read().strip()
            except IOError:
                print(f"Error reading {path}")
        return "passthrough"

class SetResolution:
    def __init__(self):
        self.E2res = None
        self.kodires = video_mode
        self.kodirate = "50Hz"
        self.port = config.av.videoport.value
        self.rate = None
        if MACHINEBRAND in ("Vu+", "Formuler"):
            resolutions = ("720i", "720p", "1080i", "1080p", "2160p")
        else:
            resolutions = ("720i", "720p", "1080i", "1080p", "2160p")
            rates = ("60Hz", "50Hz", "24Hz")
            for res in resolutions:
                for rate in rates:
                    try:
                        if avSwitch.isModeAvailable(self.port, res, rate):
                            self.kodires = res
                            self.kodirate = rate
                    except Exception:
                        pass

    def switch(self, Tokodi=False, Player=False):
        if Tokodi:
            if self.kodires and self.kodirate and self.port:
                avSwitch.setMode(self.port, self.kodires, self.kodirate)
                try:
                    with open("/proc/stb/video/videomode", "w") as f:
                        f.write(self.kodires + self.kodirate.replace("Hz", ""))
                except IOError as e:
                    print(f"Erro ao escrever no arquivo: {e}")
        else:
            if self.E2res and self.rate and self.port:
                avSwitch.setMode(self.port, self.E2res, self.rate)

    def ReadData(self):
        self.E2res = config.av.videomode[self.port].value
        self.rate = config.av.videorate[self.E2res].value
        self.switch(True)

setaudio = SetAudio()
setresolution = SetResolution()

def SaveDesktopInfo():
    global _d_dw, _d_dh
    try:
        _d_dw = getDesktop(0).size().width()
        _d_dh = getDesktop(0).size().height()
    except Exception:
        # Set resolution based on video_mode
        if video_mode in resolution_map:
            _g_dw, _g_dh = resolution_map[video_mode]
        else:
            # If video_mode is not found in resolution_map, use the default resolution
            _g_dw, _g_dh = resolution_map[""]

    print(f"[XBMC] Desktop size [{_d_dw}x{_d_dh}]")

    try:
        with open("/tmp/dw.info", "w") as f:
            f.write(f"{_d_dw}x{_d_dh}")
        chmod("/tmp/dw.info", 0o755)
    except IOError as e:
        print(f"Error saving sreen information: {e}")

SaveDesktopInfo()

def esFHD():
    # Verifica se a largura da tela é 1280x720 ou 1920x1080
    return _d_dw == 1920

def esUHD():
    # Verifica se a largura da tela é maior ou igual a 3840 (UHD/4K)
    return _d_dw >= 3840

def fhd(num, factor=1.5, uhd_factor=2.0):
    # Ajusta o valor com base na resolução
    if esUHD():
        prod = num * uhd_factor  # Aplica fator UHD (4K)
    elif esFHD():
        prod = num * factor  # Aplica fator HD
    else:
        prod = num  # Mantém o valor original para resoluções menores
    return int(round(prod))

def FBLock():
    print("[KodiLauncher] FBLock")
    fbClass.getInstance().lock()

def FBUnlock():
    print("[KodiLauncher] FBUnlock")
    fbClass.getInstance().unlock()

def RCLock():
    print("[KodiLauncher] RCLock")
    eRCInput.getInstance().lock()

def RCUnlock():
    print("[KodiLauncher] RCUnlock")
    eRCInput.getInstance().unlock()

def kodiStopped(data, retval, extraArgs):
    print(f"[KodiLauncher] kodi stopped: retval = {retval}")
    #KODI_LAUNCHER.stop()

def kodiResumeStopped(data, retval, extraArgs):
    print('[KodiLauncher] kodi resume script stopped: retval = %d' % retval)
    if retval > 0:
        KODI_LAUNCHER.stop()

class KodiVideoPlayer(InfoBarBase, InfoBarShowHide, SubsSupportStatus, SubsSupport, InfoBarSeek, InfoBarSubservicesSupport, InfoBarAspectChange, InfoBarAudioSelection, InfoBarNotifications, HelpableScreen, Screen):
    if esUHD():
        skin = """
        <screen title="custom service source" position="0, 0" size="3840,2160" zPosition="1" flags="wfNoBorder" backgroundColor="transparent">
            <widget source="global.CurrentTime" render="Label" position="3400,68" size="300,134" font="RegularHD; 64" backgroundColor="#10000000" transparent="1" zPosition="3" halign="center">
                <convert type="ClockToText">Default</convert>
            </widget>
            <eLabel name="" position="0,30" size="3844,250" zPosition="-10"/>
            <eLabel position="0,1712" zPosition="-11" size="3840,448" />
            <widget name="image" position="60,1560" size="600,600" alphatest="on" transparent="1"/>
            <widget source="session.CurrentService" render="Label" position="130,88" size="3690,76" zPosition="1" font="RegularHD;48" valign="center" halign="left" foregroundColor="#00ffa533" transparent="0">
                <convert type="ServiceName">Name</convert>
            </widget>
            <widget name="genre" position="130,172" size="3690,70" zPosition="2" font="RegularHD;38" valign="center" halign="left"/>
            <eLabel name="progressbar-back" position="686,1800" size="3000,8" backgroundColor="#33ff33" />
            <widget source="session.CurrentService" render="Progress" foregroundColor="#008A00" backgroundColor="#ffffff" position="686,1797" size="3000,16" zPosition="7" transparent="0">
                <convert type="ServicePosition">Position</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="1500,1870" size="360,134" zPosition="6" font="RegularHD;64" halign="left" transparent="0">
                <convert type="ServicePosition">Position,ShowHours</convert>
            </widget>
            <eLabel name="" text="/" position="1854,1870" size="40,134" zPosition="6" font="RegularHD;64"/>
            <widget source="session.CurrentService" render="Label" position="1904,1870" size="360,134" zPosition="6" font="RegularHD;64" halign="left" transparent="0">
                <convert type="ServicePosition">Length,ShowHours</convert>
            </widget>
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/audio.png" position="2686,1884" size="80,80" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/subtitle.png" position="2686,2040" size="80,80" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/info.png" position="1480,2040" size="80,80" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/timeslip.png" position="1850,2040" size="80,80" scale="1" alphatest="blend" />
            <eLabel name="" position="2260,1896" size="400,90" text="Audio" halign="right" font="RegularHD;40" />
            <eLabel name="" position="2260,2056" size="400,90" text="Subtitle" halign="right" font="RegularHD;40" />
            <eLabel name="" position="1580,2056" size="540,90" transparent="0" text="Info" font="RegularHD;40" />
            <eLabel name="" position="1950,2056" size="466,90" transparent="0" text="TimeSleep" font="RegularHD;40" />
            <widget source="session.CurrentService" render="Label" position="2800,1896" size="890,90" font="RegularHD;40" backgroundColor="#10000000" transparent="1">
                <convert type="TrackInfo">Audio</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="2800,2056" size="890,90" font="RegularHD;40" backgroundColor="#10000000" transparent="1">
                <convert type="TrackInfo">Subtitle</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="690,2026" size="180,60" font="RegularHD;32" halign="right" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoWidth</convert>
            </widget>
            <eLabel text="x" position="870,2026" size="48,60" font="RegularHD;32" halign="center" valign="center" transparent="0" />
            <widget source="session.CurrentService" render="Label" position="918,2026" size="180,60" font="RegularHD;32" halign="left" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoHeight</convert>
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/sd-ico.png" position="800,1904" size="160,120" render="Pixmap" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">0,640</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/hd-ico.png" position="800,1904" size="160,120" render="Pixmap" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">641,1280</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/fhd-ico.png" position="800,1904" size="160,120" render="Pixmap" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">1281,1920</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/4k-uhd-ico.png" position="800,1904" size="160,120" render="Pixmap" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">1921,3840</convert>
                <convert type="ConditionalShowHide" />
            </widget>
        </screen>"""
    elif esFHD():
        skin = """
        <screen title="custom service source" position="0, 0" size="1921,1081" zPosition="1" flags="wfNoBorder" backgroundColor="transparent">
            <widget source="global.CurrentTime" render="Label" position="1700,34" size="150,67" font="RegularHD; 32" backgroundColor="#10000000" transparent="1" zPosition="3" halign="center">
                <convert type="ClockToText">Default</convert>
            </widget>
            <eLabel name="" position="0,15" size="1924,125" zPosition="-10"/>
            <eLabel position="0,856" zPosition="-11" size="1921,224" />
            <widget name="image" position="30,780" size="300,300" alphatest="on" transparent="1"/>
            <widget source="session.CurrentService" render="Label" position="65,44" size="1845,38" zPosition="1" font="RegularHD;24" valign="center" halign="left" foregroundColor="#00ffa533" transparent="0">
                <convert type="ServiceName">Name</convert>
            </widget>
            <widget name="genre" position="65,86" size="1845,35" zPosition="2" font="RegularHD;19" valign="center" halign="left"/>
            <eLabel name="progressbar-back" position="343,900" size="1500,4" backgroundColor="#33ff33" />
            <widget source="session.CurrentService" render="Progress" foregroundColor="#008A00" backgroundColor="#ffffff" position="343,897" size="1500,10" zPosition="7" transparent="0">
                <convert type="ServicePosition">Position</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="750,935" size="180,67" zPosition="6" font="RegularHD;32" halign="left" transparent="0">
                <convert type="ServicePosition">Position,ShowHours</convert>
            </widget>
            <eLabel name="" text="/" position="927,935" size="20,67" zPosition="6" font="RegularHD;32"/>
            <widget source="session.CurrentService" render="Label" position="952,935" size="180,67" zPosition="6" font="RegularHD;32" halign="left" transparent="0">
                <convert type="ServicePosition">Length,ShowHours</convert>
            </widget>
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/audio.png" position="1343,942" size="40,40" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/subtitle.png" position="1343,1020" size="40,40" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/info.png" position="740,1020" size="40,40" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/timeslip.png" position="925,1020" size="40,40" scale="1" alphatest="blend" />
            <eLabel name="" position="1130,948" size="200,45" text="Audio" halign="right" font="RegularHD;20" />
            <eLabel name="" position="1130,1028" size="200,45" text="Subtitle" halign="right" font="RegularHD;20" />
            <eLabel name="" position="790,1028" size="270,45" transparent="0" text="Info" font="RegularHD;20" />
            <eLabel name="" position="975,1028" size="233,45" transparent="0" text="TimeSleep" font="RegularHD;20" />
            <widget source="session.CurrentService" render="Label" position="1400,948" size="445,45" font="RegularHD;20" backgroundColor="#10000000" transparent="1">
                <convert type="TrackInfo">Audio</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="1400,1028" size="445,45" font="RegularHD;20" backgroundColor="#10000000" transparent="1">
                <convert type="TrackInfo">Subtitle</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="345,1013" size="90,30" font="RegularHD;16" halign="right" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoWidth</convert>
            </widget>
            <eLabel text="x" position="435,1013" size="24,30" font="RegularHD;16" halign="center" valign="center" transparent="0" />
            <widget source="session.CurrentService" render="Label" position="462,1013" size="90,30" font="RegularHD;16" halign="left" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoHeight</convert>
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/sd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">0,640</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/hd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">641,1280</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/fhd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">1281,1920</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/4k-uhd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">1921,3840</convert>
                <convert type="ConditionalShowHide" />
            </widget>
        </screen>"""
    else:
        skin = """
        <screen title="custom service source" position="0, 0" size="1280,720" zPosition="1" flags="wfNoBorder" backgroundColor="transparent">
            <widget source="global.CurrentTime" render="Label" position="1133,22" size="100,44" font="Regular;32" backgroundColor="#10000000" transparent="1" zPosition="3" halign="center">
                <convert type="ClockToText">Default</convert>
            </widget>
            <eLabel name="" position="0,10" size="1282,83" zPosition="-10"/>
            <eLabel position="0,570" zPosition="-11" size="1280,149" />
            <widget name="image" position="20,520" size="200,200" alphatest="on" transparent="1"/>
            <widget source="session.CurrentService" render="Label" position="43,29" size="1230,25" zPosition="1" font="Regular;24" valign="center" halign="left" foregroundColor="#00ffa533" transparent="0">
                <convert type="ServiceName">Name</convert>
            </widget>
            <widget name="genre" position="43,57" size="1230,23" zPosition="2" font="Regular;19" valign="center" halign="left"/>
            <eLabel name="progressbar-back" position="228,600" size="1000,2" backgroundColor="#00cccccc" />
            <widget source="session.CurrentService" render="Progress" foregroundColor="#00007eff" backgroundColor="#00ffffff" position="228,598" size="1000,6" zPosition="7" transparent="0">
                <convert type="ServicePosition">Position</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="500,623" size="120,44" zPosition="6" font="Regular;32" halign="left" transparent="0">
                <convert type="ServicePosition">Position,ShowHours</convert>
            </widget>
            <eLabel name="" text="/" position="618,623" size="13,44" zPosition="6" font="Regular;32"/>
            <widget source="session.CurrentService" render="Label" position="634,623" size="120,44" zPosition="6" font="Regular;32" halign="left" transparent="0">
                <convert type="ServicePosition">Length,ShowHours</convert>
            </widget>
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/audio.png" position="895,628" size="27,27" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/subtitle.png" position="895,671" size="27,27" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/info.png" position="493,680" size="27,27" scale="1" alphatest="blend" />
            <ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/timeslip.png" position="617,680" size="27,27" scale="1" alphatest="blend" />
            <eLabel name="" position="753,627" size="133,30" transparent="0" text="Audio" halign="right" font="Regular;20" />
            <eLabel name="" position="753,670" size="133,30" transparent="0" text="Subtitle" halign="right" font="Regular;20" />
            <eLabel name="" position="527,681" size="180,30" transparent="0" text="Info" font="Regular;20" />
            <eLabel name="" position="650,681" size="155,30" transparent="0" text="TimeSleep" font="Regular;20" />
            <widget source="session.CurrentService" render="Label" position="933,627" size="297,30" font="Regular;20">
                <convert type="TrackInfo">Audio</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="933,670" size="297,30" font="Regular;20">
                <convert type="TrackInfo">Subtitle</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="230,675" size="60,20" font="Regular;16" halign="right" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoWidth</convert>
            </widget>
            <eLabel text="x" position="290,675" size="16,20" font="Regular;16" halign="center" valign="center" transparent="0" />
            <widget source="session.CurrentService" render="Label" position="310,675" size="60,20" font="Regular;16" halign="left" valign="center" transparent="0">
                <convert type="ServiceInfo">VideoHeight</convert>
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/sd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">0,720</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/hd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">721,1980</convert>
                <convert type="ConditionalShowHide" />
            </widget>
            <widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/4k-uhd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
                <convert type="ServiceInfo">VideoWidth</convert>
                <convert type="ValueRange">1921,4096</convert>
                <convert type="ConditionalShowHide" />
            </widget>
        </screen>"""

    RESUME_POPUP_ID = "kodiplayer_seekto"
    instance = None

    def __init__(self, session, playlistCallback, nextItemCallback, prevItemCallback, infoCallback, menuCallback):
        Screen.__init__(self, session)
        self.skinName = ['KodiVideoPlayer']
        statusScreen = self.session.instantiateDialog(StatusScreen)
        InfoBarBase.__init__(self, steal_current_service=True)
        SubsSupport.__init__(self, searchSupport=True, embeddedSupport=True)
        SubsSupportStatus.__init__(self)
        InfoBarSeek.__init__(self)
        InfoBarShowHide.__init__(self)
        InfoBarSubservicesSupport.__init__(self)
        InfoBarAspectChange.__init__(self)
        InfoBarAudioSelection.__init__(self)
        InfoBarNotifications.__init__(self)
        HelpableScreen.__init__(self)
        self.playlistCallback = playlistCallback
        self.nextItemCallback = nextItemCallback
        self.prevItemCallback = prevItemCallback
        self.infoCallback = infoCallback
        self.menuCallback = menuCallback
        self.statusScreen = statusScreen
        self.defaultImage = None
        self.postAspectChange.append(self.showAspectChanged)
        self.__timer = eTimer()
        self.__timer.callback.append(self.__seekToPosition)
        self.__image = None
        self.__thumbnail = None
        self.__position = None
        self.__firstStart = True
        self["genre"] = Label()

        # load meta info from json file provided by Kodi Enigma2Player
        try:
            meta = json.load(open(KODIEXTIN, "r"))
        except Exception as e:
            self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
            meta = {}
        self.__image = Meta(meta).getImage()
        self["image"] = WebPixmap(self.__image, caching=True)

        self.__thumbnail = Meta(meta).getThumbnail()
        self["thumbnail"] = WebPixmap(self.__thumbnail, caching=True)

        self.genre = str(", ".join(Meta(meta).getGenre()))
        self.plot = str(Meta(meta).getPlot())

        self["genre"].setText(self.genre)

        # set title, image if provided
        self.title_ref = Meta(meta).getTitle()

        # set start position if provided
        self.setStartPosition(Meta(meta).getStartTime())

        self["directionActions"] = HelpableActionMap(self, "DirectionActions",
        {
            "downUp": (playlistCallback, _("Show playlist")),
            "upUp": (playlistCallback, _("Show playlist"))
        })

        self["okCancelActions"] = HelpableActionMap(self, "OkCancelActions",
        {
            "cancel": self.close
        })

        self["actions"] = HelpableActionMap(self, "KodiPlayerActions",
        {
            "menuPressed": (menuCallback, _("Show playback menu")),
            "infoPressed": (infoCallback, _("Show playback info")),
            "nextPressed": (nextItemCallback, _("Skip to next item in playlist")),
            "prevPressed": (prevItemCallback, _("Skip to previous item in playlist")),
            "seekFwdManual": self.keyr,
            "seekBackManual": self.keyl
        })

        self.eventTracker = ServiceEventTracker(self,
        {
            iPlayableService.evStart: self.__evStart,
        })

        try:
            if KodiVideoPlayer.instance:
                 raise AssertionError("class KodiVideoPlayer is a singleton class and just one instance of this class is allowed!")
        except:
            pass

        KodiVideoPlayer.instance = self

        self.onClose.append(boundFunction(self.session.deleteDialog, self.statusScreen))
        self.onClose.append(boundFunction(Notifications.RemovePopup, self.RESUME_POPUP_ID))
        self.onClose.append(self.__timer.stop)

    def keyr(self):
        try:
            if exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.py") or exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.so"):
                from Plugins.Extensions.TimeSleep.plugin import timesleep
                timesleep(self, True)
            else:
                InfoBarSeek.seekFwdManual(self)
        except:
            InfoBarSeek.seekFwdManual(self)

    def keyl(self):
        try:
            if exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.py") or exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.so"):
                from Plugins.Extensions.TimeSleep.plugin import timesleep
                timesleep(self, False)
            else:
                InfoBarSeek.seekBackManual(self)
        except:
            InfoBarSeek.seekBackManual(self)

    def __evStart(self):
        if self.__position and self.__firstStart:
            self.__firstStart = False
            Notifications.AddNotificationWithID(self.RESUME_POPUP_ID,
                    MessageBox, _("Resuming playback"), timeout=0,
                    type=MessageBox.TYPE_INFO, enable_input=True)
            self.__timer.start(500, True)

    def __seekToPosition(self):
        if getPlayPositionInSeconds(self.session) is None:
            self.__timer.start(500, True)
        else:
            Notifications.RemovePopup(self.RESUME_POPUP_ID)
            self.doSeek(self.__position)

    def setImage(self, image):
        self.__image = image

    def setThumbnail(self, thumbnail):
        self.__thumbnail = thumbnail

    def setStartPosition(self, positionInSeconds):
        try:
            self.__position = positionInSeconds * 90 * 1000
        except Exception:
            self.__position = None

    def stopService(self):
        self.session.nav.stopService()

    def playService(self, sref):
        if self.title_ref:
            sref.setName(self.title_ref)

        self.session.nav.playService(sref)

    def audioSelection(self):
        self.session.openWithCallback(self.audioSelected, MyAudioSelection, infobar=self)

    def subtitleSelection(self):
        from Screens.AudioSelection import SubtitleSelection
        self.session.open(SubtitleSelection, self)

    def showAspectChanged(self):
        self.statusScreen.setStatus(self.getAspectStr(), "#00ff00")

    def doEofInternal(self, playing):
        self.close()

class Meta(object):
    def __init__(self, meta):
        self.meta = meta

    def getTitle(self):
        title = u""
        vTag = self.meta.get('videoInfoTag')
        if vTag:
            if vTag.get('showtitle'):
                title = vTag["showtitle"]
                episode = vTag.get("episode", -1)
                try:
                    episode = int(episode)
                except:
                    episode = -1
                season = vTag.get("season", -1)
                try:
                    season = int(season)
                except:
                    season = -1
                if season > 0 and episode > 0:
                    title += u" S%02dE%02d" % (season, episode)
                episodeTitle = vTag.get("title")
                if episodeTitle:
                    title += u" - " + episodeTitle
            else:
                title = vTag.get("title") or vTag.get("originaltitle")
                year = vTag.get("year")
                if year and title:
                    title += u" (" + str(year) + u")"
        if not title:
            title = self.meta.get("title")
        filename = self.getFilename()
        if not title and exists(str(filename) + ".spztxt"):
            f = open(str(filename) + ".spztxt", "r")
            tok = 0
            for line in f.readlines():
                idx = line.find("->")
                if idx != -1:
                    if tok == 0:
                        title = u'' + line[idx + 3:]
                        break
            f.close()
        if not title:
            listItem = self.meta.get("listItem")
            if listItem:
                title = listItem.get("label")
        return title

    def getStartTime(self):
        startTime = 0
        playerOptions = self.meta.get("playerOptions")
        if playerOptions:
            startTime = playerOptions.get("startTime", 0)
        return startTime

    def getImage(self):
        image = None
        listItem = self.meta.get("listItem")

        if listItem:
            # Retrieve the fanart information
            fanart = listItem.get("Fanart", {})
            # Get the poster and tvshowposter from the fanart
            poster = fanart.get("poster", "")
            thumb = fanart.get("thumb", "")
            imageweb = ""
            # Prefer the poster if it exists, otherwise use the tvshowposter
            if poster and poster.startswith("http"):
                imageweb = poster
            elif thumb and thumb.startswith("http"):
                imageweb = thumb
            # Check if the selected web image URL is valid
            if imageweb:
                image = imageweb  # Use the web image directly
            # If image not exits get local thumbnail from the CacheThumb element
            if image is None:
                image = ""  # Replace with static image

        return image

    def getThumbnail(self):
        thumbnail = None
        listItem2 = self.meta.get("listItem")

        if listItem2:
            # Get the local cache thumbnail from the CacheThumb element
            thumbnail = listItem2.get("CacheThumb", "")
            # If image not exits get local thumbnail from the CacheThumb element
            if thumbnail is None:
                thumbnail = ""  # Replace with static thumbnail

        return thumbnail

    def getFilename(self):
        return self.meta.get("strPath")

    def getPlot(self):
        # plot = u''
        plot = None
        vTag = self.meta.get('videoInfoTag')
        if vTag and vTag.get("plot"):
            plot = u'' + vTag.get("plot")

        filename = self.getFilename()
        if not plot and exists(str(filename) + ".spztxt"):
            f = open(str(filename) + ".spztxt", "r")
            tok = 0
            for line in f.readlines():
                idx = line.find("->")
                if idx != -1:
                    if tok == 0:
                        tok = 1
                    elif tok == 1:
                        plot = u'' + line[idx + 3:]
                        break
            f.close()

        # If image not exits get local thumbnail from the CacheThumb element
        if plot is None:
            plot = "Description not avaliable"  # Replace with static text

        return plot

    def getGenre(self):
        genre = []
        vTag = self.meta.get('videoInfoTag')
        if vTag and vTag.get("genre"):
            genre = vTag.get("genre")

        filename = self.getFilename()
        if not genre and exists(str(filename) + ".spztxt"):
            f = open(str(filename) + ".spztxt", "r")
            for line in f.readlines():
                if line.split(":")[0] == 'Género':
                    genrestr = u'' + line.split(":")[1][1:]
                    genre = genrestr.split(" | ")
                    break
            f.close()

        return genre

class VideoInfoView(Screen):
    if esUHD():
        skin = """
        <screen position="center,center" size="2300,1200" title="Video Information" >
           <!-- Thumbnail: 800x450 (proporção 16:9) -->
           <widget name="thumbnail" position="40,300" size="800,450" alphatest="on" transparent="1" valign="top" scale="1"/>
           <widget source="session.CurrentService" render="Label" position="40,40" size="2220,84" zPosition="1"  font="RegularHD;52" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
               <convert type="ServiceName">Name</convert>
           </widget>
           <widget name="genre" position="40,140" size="2220,70" zPosition="2" font="RegularHD;38" valign="center" halign="left"/>
           <eLabel name="linea" position="40,220" size="2220,4" foregroundColor="#40444444" transparent="0" zPosition="20" backgroundColor="#30555555"/>
           <!-- Descrição: 1400x800, posição X = 860 -->
           <widget name="description" position="860,300" size="1400,800" font="RegularHD; 60" render="RunningTextSpa" options="movetype=swimming,startpoint=0,direction=top,steptime=100,repeat=0,always=0,oneshot=0,startdelay=15000,pause=500,backtime=5" noWrap="0" valign="top"/>
        </screen>"""
    elif esFHD():
        skin = """
        <screen position="center,center" size="1150,600" title="Video Information" >
           <!-- Thumbnail: 400x225 (proporção 16:9) -->
           <widget name="thumbnail" position="20,150" size="400,225" alphatest="on" transparent="1" valign="top" scale="1"/>
           <widget source="session.CurrentService" render="Label" position="20,20" size="1110,42" zPosition="1"  font="RegularHD;26" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
               <convert type="ServiceName">Name</convert>
           </widget>
           <widget name="genre" position="20,70" size="1110,35" zPosition="2" font="RegularHD;19" valign="center" halign="left"/>
           <eLabel name="linea" position="20,110" size="1110,2" foregroundColor="#40444444" transparent="0" zPosition="20" backgroundColor="#30555555"/>
           <!-- Descrição: 700x400, posição X = 430 -->
           <widget name="description" position="430,150" size="700,400" font="RegularHD; 30" render="RunningTextSpa" options="movetype=swimming,startpoint=0,direction=top,steptime=100,repeat=0,always=0,oneshot=0,startdelay=15000,pause=500,backtime=5" noWrap="0" valign="top"/>
        </screen>"""
    else:
        skin = """
        <screen position="center,center" size="766,400" title="Video Information" >
           <!-- Thumbnail: 300x169 (proporção 16:9) -->
           <widget name="thumbnail" position="13,100" size="300,169" alphatest="on" transparent="1" valign="top" scale="1"/>
           <widget source="session.CurrentService" render="Label" position="13,13" size="740,28" zPosition="1"  font="Regular;26" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
               <convert type="ServiceName">Name</convert>
           </widget>
           <widget name="genre" position="13,46" size="740,23" zPosition="2" font="Regular;19" valign="center" halign="left"/>
           <eLabel name="linea" position="13,73" size="740,1" foregroundColor="#40444444" transparent="0" zPosition="20" backgroundColor="#30555555"/>
           <!-- Descrição: 433x266, posição X = 323 -->
           <widget name="description" position="323,100" size="433,266" font="Regular; 30" render="RunningTextSpa" options="movetype=swimming,startpoint=0,direction=top,steptime=100,repeat=0,always=0,oneshot=0,startdelay=15000,pause=500,backtime=5" noWrap="0" valign="top"/>
        </screen>"""

    def __init__(self, session):
        self.skin = VideoInfoView.skin
        Screen.__init__(self, session)

        self["genre"] = Label()
        self["description"] = Label()
        # load meta info from json file provided by Kodi Enigma2Player
        try:
            meta = json.load(open(KODIEXTIN, "r"))
        except Exception as e:
            self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
            meta = {}
        self.__thumbnail = Meta(meta).getThumbnail()
        self["thumbnail"] = WebPixmap(self.__thumbnail, caching=True)

        self.genre = str(", ".join(Meta(meta).getGenre()))
        self.plot = str(Meta(meta).getPlot())

        self["genre"].setText(self.genre)
        self["description"].setText(self.plot)

        self["actions"] = ActionMap(["OkCancelActions"],
        {
                "cancel": self.close,
                "ok": self.close
        }, -1)

class E2KodiExtRequestHandler(KodiExtRequestHandler):

    def handle_request(self, opcode, status, data):
        self.server.messageOut.put((status, data))
        self.server.messagePump.send(opcode)
        return self.server.messageIn.get()

class E2KodiExtServer(UDSServer):
    def __init__(self):
        UDSServer.__init__(self, KODIEXT_SOCKET, E2KodiExtRequestHandler)
        self.kodiPlayer = None
        self.subtitles = []
        self.messageIn = Queue()
        self.messageOut = Queue()
        self.messagePump = ePythonMessagePump()
        self.messagePump.recv_msg.get().append(self.messageReceived)

    def shutdown(self):
        self.messagePump.stop()
        self.messagePump = None
        UDSServer.shutdown(self)

    def messageReceived(self, opcode):
        status, data = self.messageOut.get()
        if opcode == OP_CODE_EXIT:
            self.handleExitMessage(status, data)
        elif opcode == OP_CODE_PLAY:
            self.handlePlayMessage(status, data)
        elif opcode == OP_CODE_PLAY_STATUS:
            self.handlePlayStatusMessage(status, data)
        elif opcode == OP_CODE_PLAY_STOP:
            self.handlePlayStopMessage(status, data)
        elif opcode == OP_CODE_SWITCH_TO_ENIGMA2:
            self.handleSwitchToEnigma2Message(status, data)
        elif opcode == OP_CODE_SWITCH_TO_KODI:
            self.handleSwitchToKodiMessage(status, data)

    def handleExitMessage(self, status, data):
        self.messageIn.put((True, None))
        self.stopTimer = eTimer()
        self.stopTimer.callback.append(KODI_LAUNCHER.stop)
        self.stopTimer.start(500, True)

    def handlePlayStatusMessage(self, status, data):
        position = getPlayPositionInSeconds(SESSION)
        duration = getDurationInSeconds(SESSION)
        if position and duration:
            # decoder sometimes provides invalid position after seeking
            if position > duration:
                position = None
        statusMessage = {
            "duration": duration,
            "playing": self.kodiPlayer is not None,
            "position": position}
        self.messageIn.put((self.kodiPlayer is not None, json.dumps(statusMessage)))

    def handlePlayStopMessage(self, status, data):
        FBLock()
        RCLock()
        self.messageIn.put((True, None))

    def handleSwitchToEnigma2Message(self, status, data):
        self.messageIn.put((True, None))
        self.stopTimer = eTimer()
        self.stopTimer.callback.append(KODI_LAUNCHER.stop)
        self.stopTimer.start(500, True)

    def handleSwitchToKodiMessage(self, status, data):
        self.messageIn.put((True, None))

    def handlePlayMessage(self, status, data):
        if data is None:
            self.logger.error("handlePlayMessage: no data!")
            self.messageIn.put((False, None))
            return
        FBUnlock()
        RCUnlock()

        setaudio.switch(False, True)
        if MACHINEBRAND not in ('Vu+', 'Formuler'):
            setresolution.switch(False, True)
        # parse subtitles, play path and service type from data
        sType = 4097
        subtitles = []
        if isinstance(data, bytes):
            data = data.decode()
        dataSplit = data.strip().split("\n")
        if len(dataSplit) == 1:
            playPath = dataSplit[0]
        if len(dataSplit) == 2:
            playPath, subtitlesStr = dataSplit
            subtitles = subtitlesStr.split("|")
        elif len(dataSplit) >= 3:
            playPath, subtitlesStr, sTypeStr = dataSplit[:3]
            subtitles = subtitlesStr.split("|")
            try:
                sType = int(sTypeStr)
            except ValueError:
                self.logger.error("handlePlayMessage: '%s' is not a valid servicetype",
                        sType)
        if playPath.startswith('http'):
            playPathSplit = playPath.split("|")
            if len(playPathSplit) > 1:
                playPath = playPathSplit[0] + "#" + playPathSplit[1]
        self.logger.debug("handlePlayMessage: playPath = %s", playPath)
        for idx, subtitlesPath in enumerate(subtitles):
            self.logger.debug("handlePlayMessage: subtitlesPath[%d] = %s", idx, subtitlesPath)

        # load meta info from json file provided by Kodi Enigma2Player
        try:
            meta = json.load(open(KODIEXTIN, "r"))
        except Exception as e:
            self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
            meta = {}
        else:
            if meta.get("strPath") and meta["strPath"] not in data:
                self.logger.error("meta data for another filepath?")
                meta = {}

        # create Kodi player Screen
        noneFnc = lambda: None
        self.kodiPlayer = SESSION.openWithCallback(self.kodiPlayerExitCB, KodiVideoPlayer,
            noneFnc, noneFnc, noneFnc, self.infoview, noneFnc)

        # load subtitles
        if len(subtitles) > 0 and hasattr(self.kodiPlayer, "loadSubs"):
            # TODO allow to play all subtitles
            subtitlesPath = subtitles[0]
            self.kodiPlayer.loadSubs(subtitlesPath)

        # create service reference
        sref = eServiceReference(sType, 0, playPath)

        # set title, image if provided
        title = Meta(meta).getTitle()
        if not title:
            title = basename(playPath.split("#")[0])
        sref.setName(title)

        self.kodiPlayer.playService(sref)
        self.messageIn.put((True, None))

    def kodiPlayerExitCB(self, callback=None):
        setaudio.switch(True, True)
        if MACHINEBRAND not in ('Vu+', 'Formuler'):
            setresolution.switch(True, True)
        SESSION.nav.stopService()
        self.kodiPlayer = None
        self.subtitles = []

    def infoview(self):
        SESSION.open(VideoInfoView)

class KodiLauncher(Screen):
    skin = """<screen position="fill" backgroundColor="#FF000000" flags="wfNoBorder" title=" "></screen>"""

    def __init__(self, session):
        Screen.__init__(self, session)
        RCLock()
        self.previousService = self.session.nav.getCurrentlyPlayingServiceReference()
        self.session.nav.stopService()
        self.startupTimer = eTimer()
        self.startupTimer.timeout.get().append(self.startup)
        self.startupTimer.start(500, True)
        self.onClose.append(RCUnlock)

    def startup(self):
        def psCallback(data, retval, extraArgs):
            FBLock()
            kodiProc = None
            if isinstance(data, bytes):
                data = data.decode()
            procs = data.split("\n")
            if len(procs) > 0:
                for p in procs:
                    if "kodi.bin" in p:
                        if kodiProc is not None:
                            print("[KodiLauncher] startup - there are more kodi processes running!")
                            return self.stop()
                        kodiProc = p.split()
            if kodiProc is not None:
                kodiPid = int(kodiProc[0])
                print("[KodiLauncher] startup: kodi is running, pid = %d , resuming..." % kodiPid)
                self.resumeKodi(kodiPid)
            else:
                print("[KodiLauncher] startup: kodi is not running, starting...")
                self.startKodi()

        self._checkConsole = Console()
        self._checkConsole.ePopen("ps | grep kodi.bin | grep -v grep", psCallback)

    def startKodi(self):
        self._startConsole = Console()
        self._startConsole.ePopen(KODIRUN_SCRIPT, kodiStopped)

    def resumeKodi(self, pid):
        self._resumeConsole = Console()
        self._resumeConsole.ePopen(KODIRESUME_SCRIPT % pid, kodiResumeStopped)

    def stop(self):
        FBUnlock()
        setaudio.switch()
        setresolution.switch()
        if self.previousService:
            self.session.nav.playService(self.previousService)
        try:
            if exists("/media/hdd/.kodi/"):
                system("rm -rf /media/hdd/kodi_crashlog*.log")
            else:
                system("rm -rf /tmp/kodi/kodi_crashlog*.log")
        except OSError:
            pass
        self.close()

def autoStart(reason, **kwargs):
    print("[KodiLauncher] autoStart - reason = %d" % reason)
    global SERVER_THREAD
    global SERVER
    if reason == 0:
        try:
            remove(KODIEXT_SOCKET)
        except OSError:
            pass
        SERVER = E2KodiExtServer()
        SERVER_THREAD = threading.Thread(target=SERVER.serve_forever)
        SERVER_THREAD.start()
    elif reason == 1:
        SERVER.shutdown()
        SERVER_THREAD.join()

def startLauncher(session, **kwargs):
    if config.kodi.standalone.value:
        session.open(TryQuitMainloop, retvalue=QUIT_KODI)
    else:
        setaudio.ReadData()
        # setaudio.switch(True)
        setresolution.ReadData()
        eRCInput.getInstance().unlock()
        global SESSION
        SESSION = session
        global KODI_LAUNCHER
        KODI_LAUNCHER = session.open(KodiLauncher)

def startMenuLauncher(menuid, **kwargs):
    if menuid == "mainmenu":
        return [("Kodi", startLauncher, "kodi", 1)]
    return []

class KodiExtSetup(Setup):
    def __init__(self, session):
        Setup.__init__(self, session, "Kodi", plugin="Extensions/Kodi")
        self["key_blue"] = StaticText(_("Start Kodi"))
        self["actions"] = HelpableActionMap(self, ["ColorActions"], {
            "blue": (self.startKodi, _("Start Kodi"))
        }, prio=-1, description=_("Kodi Actions"))

    def startKodi(self):
        self.close(True)

def startSetup(session, **kwargs):
    def kodiSetupCallback(result=None):
        if result and result is True:
            startLauncher(session)
    session.openWithCallback(kodiSetupCallback, KodiExtSetup)

def Plugins(**kwargs):
    kodiext = "kodiext_FHD.png" if _g_dw and _g_dw >= 1920 else "kodiext_HD.png"
    l = [
        PluginDescriptor("Kodi", PluginDescriptor.WHERE_AUTOSTART, "Kodi Launcher", fnc=autoStart),
        PluginDescriptor("Kodi", PluginDescriptor.WHERE_PLUGINMENU, "Kodi Settings", icon=kodiext, fnc=startSetup)
      ]
    if config.kodi.addToMainMenu.value:
        l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_MENU, fnc=startMenuLauncher))
    if config.kodi.addToExtensionMenu.value:
        l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_EXTENSIONSMENU, icon=kodiext, fnc=startLauncher))
    return l
