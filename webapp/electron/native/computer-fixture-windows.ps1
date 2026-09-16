param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = "Stop"
Add-Type -OutputAssembly $Destination -OutputType WindowsApplication -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Windows.Forms;
public static class NativeFixture {
    [STAThread] public static void Main() {
        Application.EnableVisualStyles();
        var form = new Form { Text = "Marionette computer fixture", ClientSize = new Size(420, 260) };
        var input = new TextBox { AccessibleName = "Fixture name", Location = new Point(30, 75), Width = 270 };
        var password = new TextBox { UseSystemPasswordChar = true, Text = "NEVER_SNAPSHOT_PASSWORD", Location = new Point(30, 30), Width = 270 };
        var result = new Label { Text = "Not saved", Location = new Point(30, 175), Width = 330 };
        var button = new Button { Text = "Apply fixture", Location = new Point(30, 125), Width = 160 };
        button.Click += (sender, args) => result.Text = "Saved " + input.Text;
        form.Controls.AddRange(new Control[] { input, password, result, button });
        Application.Run(form);
    }
}
'@
