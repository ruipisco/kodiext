from os import system, chmod
from os.path import exists
from enigma import eTimer, fbClass, eRCInput, getDesktop, eDVBVolumecontrol

from Components.AVSwitch import avSwitch
from Components.config import config, ConfigSubsection, ConfigYesNo
from Components.ActionMap import HelpableActionMap
from Components.Console import Console
from Components.PluginComponent import PluginDescriptor
from Components.Sources.StaticText import StaticText
from Components.SystemInfo import BoxInfo

from Screens.Screen import Screen
from Screens.Setup import Setup
from Screens.Standby import QUIT_KODI, TryQuitMainloop
from Tools.Directories import fileWriteLine

config.kodi = ConfigSubsection()
config.kodi.addToMainMenu = ConfigYesNo(False)
config.kodi.addToExtensionMenu = ConfigYesNo(True)
config.kodi.standalone = ConfigYesNo(False)

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

	def switch(self):
		self.volctrl.setVolume(self.VolPrev, self.VolPrev)

		if BoxInfo.getItem("CanDownmixAC3"):
			fileWriteLine("/proc/stb/audio/ac3", self.ac3)

		if BoxInfo.getItem("CanDownmixDTS"):
			fileWriteLine("/proc/stb/audio/dts", self.dts)

		if BoxInfo.getItem("CanDownmixAAC"):
			fileWriteLine("/proc/stb/audio/aac", self.aac)

		if BoxInfo.getItem("CanDownmixAACPlus"):
			fileWriteLine("/proc/stb/audio/aacplus", self.aacplus)

	def ReadData(self):
		self.VolPrev = self.volctrl.getVolume()
		self.VolPlayer = self.VolPrev
		if BoxInfo.getItem("CanDownmixAC3"):
			try:
				self.ac3 = open("/proc/stb/audio/ac3", "r").read()
			except:
				pass

		if BoxInfo.getItem("CanDownmixDTS"):
			try:
				self.dts = open("/proc/stb/audio/dts", "r").read()
			except:
				pass

		if BoxInfo.getItem("CanDownmixAAC"):
			try:
				self.aac = open("/proc/stb/audio/aac", "r").read()
			except:
				pass

		if BoxInfo.getItem("CanDownmixAACPlus"):
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
		if MACHINEBRAND in ("Vu+", "Formuler"):
			resolutions = ("720i", "720p")
		else:
			resolutions = ("720i", "720p", "1080i", "1080p")
			rates = ("60Hz", "50Hz")
			for res in resolutions:
				for rate in rates:
					try:
						if avSwitch.isModeAvailable(self.port, res, rate):
							self.kodires = res
							self.kodirate = rate
					except Exception:
						pass

	def switch(self):
		if self.E2res and self.rate and self.port:
			avSwitch.setMode(self.port, self.E2res, self.rate)

	def ReadData(self):
		self.E2res = config.av.videomode[self.port].value
		self.rate = config.av.videorate[self.E2res].value


setaudio = SetAudio()
setresolution = SetResolution()


def SaveDesktopInfo():
	global _g_dw, _g_dh
	try:
		_g_dw = getDesktop(0).size().width()
		_g_dh = getDesktop(0).size().height()
	except Exception:
		_g_dw, _g_dh = 1280, 720
	print(f"[XBMC] Desktop size [{_g_dw}x{_g_dh}]")
	fileWriteLine("/tmp/dw.info", f"{_g_dw}x{_g_dh}")
	chmod("/tmp/dw.info", 0o755)


SaveDesktopInfo()


def kodiStopped(data, retval, extraArgs):
	print(f"[KodiLauncher] kodi stopped: retval = {retval}")
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
			procs = data.split("\n")
			if len(procs) > 0:
				for p in procs:
					if "kodi.bin" in p:
						if kodiProc is not None:
							print("[KodiLauncher] startup - there are more kodi processes running!")
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
			if exists("/media/hdd/.kodi/"):
				system("rm -rf /media/hdd/kodi_crashlog*.log")
			else:
				system("rm -rf /tmp/kodi/kodi_crashlog*.log")
		except OSError:
			pass
		self.close()


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
	screenwidth = getDesktop(0).size().width()
	kodiext = "kodiext_FHD.png" if screenwidth and screenwidth == 1920 else "kodiext_HD.png"
	l = [PluginDescriptor("Kodi", PluginDescriptor.WHERE_PLUGINMENU, "Kodi Settings", icon=kodiext, fnc=startSetup)]
	if config.kodi.addToMainMenu.value:
		l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_MENU, fnc=startMenuLauncher))
	if config.kodi.addToExtensionMenu.value:
		l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_EXTENSIONSMENU, icon=kodiext, fnc=startLauncher))
	return l
