import os

from Components.Console import Console
from Components.PluginComponent import PluginDescriptor
from Screens.Screen import Screen
from Screens.Setup import Setup
from Tools.Directories import fileExists

from Components.config import config, ConfigSubsection, ConfigYesNo
from Components.AVSwitch import iAVSwitch

from enigma import eTimer, fbClass, eRCInput, getDesktop, eDVBVolumecontrol
from Components.SystemInfo import SystemInfo


config.kodi = ConfigSubsection()
config.kodi.addToMainMenu = ConfigYesNo(False)
config.kodi.addToExtensions = ConfigYesNo(True)
config.kodi.standalone = ConfigYesNo(False)

from Components.SystemInfo import BoxInfo
MACHINEBRAND = BoxInfo.getItem("displaybrand")

KODI_LAUNCHER = None

SESSION = None

_g_dw, _g_dh = 1280, 720


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
        if Tokodi:
            if Player:
                self.VolPlayer = self.volctrl.getVolume()
            vol = 100
            ac3 = "downmix"
            dts = "downmix"
            aac = "passthrough"
            aacplus = "passthrough"
        else:
            if Player:
                vol = self.VolPlayer
            else:
                vol = self.VolPrev
            ac3 = self.ac3
            dts = self.dts
            aac = self.aac
            aacplus = self.aacplus

        self.volctrl.setVolume(vol, vol)

        if SystemInfo["CanDownmixAC3"]:
            try:
                open("/proc/stb/audio/ac3", "w").write(ac3)
            except:
                pass

        if SystemInfo["CanDownmixDTS"]:
            try:
                open("/proc/stb/audio/dts", "w").write(dts)
            except:
                pass

        if SystemInfo["CanDownmixAAC"]:
            try:
                open("/proc/stb/audio/aac", "w").write(aac)
            except:
                pass

        if SystemInfo["CanDownmixAACPlus"]:
            try:
                open("/proc/stb/audio/aacplus", "w").write(aacplus)
            except:
                pass

    def ReadData(self):
        self.VolPrev = self.volctrl.getVolume()
        self.VolPlayer = self.VolPrev
        if SystemInfo["CanDownmixAC3"]:
            try:
                self.ac3 = open("/proc/stb/audio/ac3", "r").read()
            except:
                pass

        if SystemInfo["CanDownmixDTS"]:
            try:
                self.dts = open("/proc/stb/audio/dts", "r").read()
            except:
                pass

        if SystemInfo["CanDownmixAAC"]:
            try:
                self.aac = open("/proc/stb/audio/aac", "r").read()
            except:
                pass

        if SystemInfo["CanDownmixAACPlus"]:
            try:
                self.aacplus = open("/proc/stb/audio/aacplus", "r").read()
            except:
                pass


class SetResolution:
    def __init__(self):
        self.E2res = None
        self.kodires = "720p"
        self.kodirate = "50Hz"
        self.port = config.av.videoport.value
        self.rate = None
        if MACHINEBRAND in ('Vu+', 'Formuler'):
            resolutions = ("720i", "720p")
        else:
            resolutions = ("720i", "720p", "1080i", "1080p")
            rates = ("60Hz", "50Hz")
            for res in resolutions:
                for rate in rates:
                    try:
                        if iAVSwitch.isModeAvailable(self.port, res, rate):
                            self.kodires = res
                            self.kodirate = rate
                    except:
                        pass

    def switch(self, Tokodi=False, Player=False):
        if Tokodi:
            if self.kodires and self.kodirate and self.port:
                iAVSwitch.setMode(self.port, self.kodires, self.kodirate)
                open("/proc/stb/video/videomode", "w").write(self.kodires + self.kodirate.replace("Hz", ""))
        else:
            if self.E2res and self.rate and self.port:
                iAVSwitch.setMode(self.port, self.E2res, self.rate)

    def ReadData(self):
        self.E2res = config.av.videomode[self.port].value
        self.rate = config.av.videorate[self.E2res].value
        self.switch(True)


setaudio = SetAudio()
setresolution = SetResolution()


def SaveDesktopInfo():
    global _g_dw, _g_dh
    try:
        _g_dw = getDesktop(0).size().width()
        _g_dh = getDesktop(0).size().height()
    except:
        _g_dw, _g_dh = 1280, 720
    print("[XBMC] Desktop size [%dx%d]" % (_g_dw, _g_dh))
    if not fileExists('/tmp/dw.info'):
        os.system('touch /tmp/dw.info')
    os.system('chmod 755 /tmp/dw.info')
    open("/tmp/dw.info", "w").write(str(_g_dw) + "x" + str(_g_dh))


SaveDesktopInfo()


def kodiStopped(data, retval, extraArgs):
    print('[KodiLauncher] kodi stopped: retval = %d' % retval)
    KODI_LAUNCHER.stop()


class KodiLauncher(Screen):
    skin = """<screen position="fill" backgroundColor="#FF000000" flags="wfNoBorder" title=" "></screen>"""

    def __init__(self, session):
        eRCInput.getInstance().lock()
        Screen.__init__(self, session)
        self.previousService = self.session.nav.getCurrentlyPlayingServiceReference()
        self.session.nav.stopService()
        self.startupTimer = eTimer()
        self.startupTimer.timeout.get().append(self.startup)
        self.startupTimer.start(500, True)
        self.onClose.append(self.rcUnlock)

    def rcUnlock(self):
        eRCInput.getInstance().unlock()

    def startup(self):
        def psCallback(data, retval, extraArgs):
            fbClass.getInstance().lock()
            kodiProc = None
            if isinstance(data, bytes):
                data = data.decode()
            procs = data.split('\n')
            if len(procs) > 0:
                for p in procs:
                    if 'kodi.bin' in p:
                        if kodiProc is not None:
                            print('[KodiLauncher] startup - there are more kodi processes running!')
                            return self.stop()
                        kodiProc = p.split()
            print("[KodiLauncher] startup: kodi is not running, starting...")
            self.startKodi()

        self._checkConsole = Console()
        self._checkConsole.ePopen("ps | grep kodi.bin | grep -v grep", psCallback)

    def startKodi(self):
        self._startConsole = Console()
        self._startConsole.ePopen("/usr/bin/kodi", kodiStopped)

    def stop(self):
        fbClass.getInstance().unlock()
        setaudio.switch()
        setresolution.switch()
        if self.previousService:
            self.session.nav.playService(self.previousService)
        try:
            if os.path.exists('/media/hdd/.kodi/'):
                os.system('rm -rf /media/hdd/kodi_crashlog*.log')
            else:
                os.system('rm -rf /tmp/kodi/kodi_crashlog*.log')
        except:
            pass
        self.close()


def startLauncher(session, **kwargs):
    setaudio.ReadData()
    setaudio.switch(True)
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


def startSetup(session, **kwargs):
    session.open(KodiExtSetup)


def Plugins(**kwargs):
    screenwidth = getDesktop(0).size().width()
    kodiext = 'kodiext_FHD.png' if screenwidth and screenwidth == 1920 else 'kodiext_HD.png'
    l = [PluginDescriptor("Kodi", PluginDescriptor.WHERE_PLUGINMENU, "Kodi Launcher", icon=kodiext, fnc=startSetup)]
    if config.kodi.addToMainMenu.value:
        l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_MENU, fnc=startMenuLauncher))
    if config.kodi.addToExtensions.value:
        l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_EXTENSIONSMENU, icon=kodiext, fnc=startLauncher))
    return l
