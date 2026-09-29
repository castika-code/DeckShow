// Windows launcher for Castika DeckShow. Not for direct use: install.ps1 compiles
// this with the C# compiler that ships inside Windows (.NET Framework 4.x,
// C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe), the way the macOS
// installer builds its applet with osacompile: built on the user's machine, so
// nothing signed or downloaded has to be trusted, and the process carries our
// name (Castika.DeckShow.exe) and icon (installer/deckshow_icon.ico, compiled in
// with /win32icon) in Task Manager, Startup apps and the microphone privacy list.
//
// The same executable is copied to two places and picks its role by location:
//   <package>\Castika.DeckShow.exe                       host: runs deckshow.py
//        host (audio daemon + Companion adapter in one process) and restarts
//        it if it dies (the Startup shortcut starts this at login)
//   <package>\streamdeck\<plugin>.sdPlugin\bin\Castika.DeckShow.exe
//        plugin: started by the Stream Deck app with -port/-pluginUUID/
//        -registerEvent/-info, passed straight through to deckshow.py plugin
// Python is the install's own venv; its pythonw.exe is copied as
// .venv\Scripts\Castika.DeckShow.exe so the microphone list shows that name.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

static class Launcher
{
    static string root;
    static string python;

    // The Stream Deck app starts the plugin through the directory junction in
    // its own Plugins folder, so the path this process sees is that junction and
    // not the install folder: walking up from it lands in the app's Plugins
    // folder, where there is no Python. Ask Windows for the real path first.
    const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sec,
                                     uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern uint GetFinalPathNameByHandleW(IntPtr file, StringBuilder path, uint count, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);

    static string RealPath(string path)
    {
        IntPtr h = CreateFileW(path, 0, 7, IntPtr.Zero, 3 /* OPEN_EXISTING */, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero);
        if (h == new IntPtr(-1)) return path;
        try
        {
            var sb = new StringBuilder(1024);
            if (GetFinalPathNameByHandleW(h, sb, 1024, 0) == 0) return path;
            string s = sb.ToString();
            return s.StartsWith(@"\\?\") ? s.Substring(4) : s;
        }
        finally { CloseHandle(h); }
    }

    static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception e)
        {
            // Without this the process just vanishes and the Stream Deck app can
            // only report "Process stopped (unexpected)".
            string where = root != null ? Path.Combine(root, "logs") : Path.GetTempPath();
            try { Directory.CreateDirectory(where); } catch { where = Path.GetTempPath(); }
            try { File.AppendAllText(Path.Combine(where, "launcher_error.log"), DateTime.Now + " " + e + Environment.NewLine); } catch { }
            return 1;
        }
    }

    static int Run(string[] args)
    {
        string exeDir = RealPath(AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\')).TrimEnd('\\');
        bool pluginRole = string.Equals(Path.GetFileName(exeDir), "bin", StringComparison.OrdinalIgnoreCase);
        root = pluginRole ? Path.GetFullPath(Path.Combine(exeDir, "..", "..", "..")) : exeDir;
        python = Path.Combine(root, ".venv", "Scripts", "Castika.DeckShow.exe");
        if (!File.Exists(python)) python = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(python)) throw new FileNotFoundException("no Python in the install folder: " + root, python);
        Directory.CreateDirectory(Path.Combine(root, "logs"));

        if (pluginRole)
        {
            Process p = Start("plugin", args, "plugin_launcher.log");
            p.WaitForExit();
            return p.ExitCode;
        }

        // Host role: deckshow.py host (audio daemon + Companion adapter in one
        // process). Kept alive: if it exits, start it again after a pause. This
        // loop is the one thing that must never end: it is what brings the show
        // back without the user having to start anything by hand.
        while (true)
        {
            try
            {
                Process host = Start("host", new string[0], "host_launcher.log");
                host.WaitForExit();
            }
            catch (Exception e)
            {
                try { Log("host_launcher.log").WriteLine(DateTime.Now + " could not start the host: " + e.Message); }
                catch { }
            }
            Thread.Sleep(10000);
        }
    }

    // One writer per process, opened so that a second attempt may open the same
    // file: reopening it on every restart is what made the keep-alive loop die on
    // its first restart instead of bringing the host back (2026-09-28).
    static StreamWriter logFile;

    static StreamWriter Log(string logName)
    {
        if (logFile == null)
        {
            var fs = new FileStream(Path.Combine(root, "logs", logName), FileMode.Append,
                                    FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
            logFile = new StreamWriter(fs);
            logFile.AutoFlush = true;
        }
        return logFile;
    }

    static Process Start(string mode, string[] args, string logName)
    {
        var psi = new ProcessStartInfo();
        psi.FileName = python;
        psi.Arguments = Quote(Path.Combine(root, "deckshow.py")) + " " + mode;
        foreach (string a in args) psi.Arguments += " " + Quote(a);
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        var p = Process.Start(psi);
        // The scripts write their own log files; only what escapes to stdout/stderr
        // (a crash before logging is set up) lands in logs\<logName>.
        var log = Log(logName);
        p.OutputDataReceived += (s, e) => { if (e.Data != null) log.WriteLine(e.Data); };
        p.ErrorDataReceived += (s, e) => { if (e.Data != null) log.WriteLine(e.Data); };
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        return p;
    }

    static string Quote(string s)
    {
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }
}
