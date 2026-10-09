using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("墨流 InkFlow")]
[assembly: AssemblyProduct("墨流 InkFlow")]
[assembly: AssemblyCompany("InkFlow")]
[assembly: AssemblyDescription("墨流本地开发版启动程序")]
[assembly: AssemblyVersion("0.7.4.0")]

internal static class InkFlowLauncher
{
    [STAThread]
    private static void Main()
    {
        try
        {
            string directory = AppDomain.CurrentDomain.BaseDirectory;
            string script = File.ReadAllText(Path.Combine(directory, "launch-script.txt")).Trim();
            if (!Path.IsPathRooted(script) || !File.Exists(script)
                || Path.GetFileName(script) != "start-desktop-hidden.ps1" || script.Contains("\""))
                throw new IOException("本地启动路径无效，请从源码目录重新登记墨流入口。");
            Process.Start(new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + script + "\"",
                WorkingDirectory = Path.GetDirectoryName(script),
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            });
        }
        catch (Exception error)
        {
            MessageBox.Show("墨流未能启动：" + error.Message, "墨流 InkFlow", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
