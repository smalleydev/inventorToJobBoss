'==============================================================================
' StandardAddInServer.vb
' BomPushAddIn — Inventor 2026 Add-In entry point (.NET 8)
'
' Registers a "Push BOM" ribbon button on the Assembly > Tools tab. Clicking
' the button extracts the active assembly's structured BOM (via BomExtractor)
' and writes deduplicated CSV + JSON output files.
'
' Loading model: Inventor 2025+ hosts .NET 8 directly and discovers this
' class through the .addin manifest (ClassId must match the Guid attribute
' below). No COM registration / regasm / strong-name signing is required.
'==============================================================================

Imports Inventor
Imports System.Runtime.InteropServices

<ComVisible(True)>
<Guid(StandardAddInServer.AddInClientId)>
Public Class StandardAddInServer
    Implements Inventor.ApplicationAddInServer

    ' Single source of truth for the add-in GUID. Must match ClassId/ClientId
    ' in BomPushAddIn.addin (manifest uses braces, attribute does not).
    Public Const AddInClientId As String = "32A24226-7D2C-4A55-84DB-98868513CBFE"
    Private Const AddInClientIdBraced As String = "{" & AddInClientId & "}"

    ' Output directory for the RAW extracted BOM (before JL Check review).
    ' Deliberately NOT the same folder as JL Check's approved-export
    ' inbox (C:\TEMP\jb_inbox) — the watcher service only ever watches
    ' that folder and expects finalized/approved exports there, not raw,
    ' unresolved BOM data straight off the CAD model.
    Private Const OutputDirectory As String = "C:\TEMP"

    ' Reference to the running Inventor session, captured in Activate.
    Private m_inventorApplication As Inventor.Application

    ' IMPORTANT: This must be a class-level WithEvents field, not a local
    ' variable. If the ButtonDefinition reference is method-local, the GC
    ' collects it after Activate returns and the OnExecute event silently
    ' stops firing (button stays visible but does nothing). WithEvents +
    ' the Handles clause on OnPushBomButton keeps the reference alive and
    ' wires the event declaratively.
    Private WithEvents m_oButtonDef As ButtonDefinition

    ''' <summary>
    ''' Called once by Inventor when the add-in loads. Builds the ribbon UI.
    ''' </summary>
    Public Sub Activate(ByVal addInSiteObject As Inventor.ApplicationAddInSite, ByVal firstTime As Boolean) _
        Implements Inventor.ApplicationAddInServer.Activate

        m_inventorApplication = addInSiteObject.Application

        Try
            Dim oControlDefs As ControlDefinitions =
                m_inventorApplication.CommandManager.ControlDefinitions

            ' kNonShapeEditCmdType is required for a general-purpose action
            ' button; kShapeEditCmdType is for sketch/geometry-editing
            ' commands and will not fire correctly in this context.
            m_oButtonDef = oControlDefs.AddButtonDefinition(
                "Push BOM to JobBOSS",              ' Display name
                "BomPushAddIn:PushBomButton",       ' Internal name — must be unique across all add-ins
                CommandTypesEnum.kNonShapeEditCmdType,
                AddInClientIdBraced,                ' Associates the command with this add-in
                "Extracts assembly BOM and pushes to JobBOSS quote",  ' Tooltip
                "Push BOM"                          ' Button caption
            )

            ' Target: Assembly document ribbon > built-in Tools tab.
            Dim oRibbon As Ribbon = m_inventorApplication.UserInterfaceManager.Ribbons.Item("Assembly")
            Dim oTab As RibbonTab = oRibbon.RibbonTabs.Item("id_TabTools")

            ' Reuse the panel if it already exists (e.g. add-in reloaded
            ' without a full Inventor restart); otherwise create it.
            Dim oPanel As RibbonPanel
            Try
                oPanel = oTab.RibbonPanels.Item("BomPushPanel")
            Catch
                oPanel = oTab.RibbonPanels.Add("JobBOSS BOM", "BomPushPanel", AddInClientIdBraced)
            End Try

            oPanel.CommandControls.AddButton(m_oButtonDef, True)

        Catch ex As Exception
            ' Surface ribbon-construction failures loudly; Inventor gives no
            ' feedback of its own when an add-in's Activate throws.
            MsgBox("BomPushAddIn failed to initialize: " & ex.Message & vbCrLf & ex.StackTrace,
                   MsgBoxStyle.Critical, "BomPushAddIn")
        End Try

    End Sub

    ''' <summary>
    ''' Ribbon button click handler. Extracts, dedupes, and writes the BOM.
    ''' </summary>
    Private Sub OnPushBomButton(ByVal Context As NameValueMap) Handles m_oButtonDef.OnExecute

        Try
            Dim oDoc As AssemblyDocument = TryCast(m_inventorApplication.ActiveDocument, AssemblyDocument)
            If oDoc Is Nothing Then
                MsgBox("Open an assembly document before pushing a BOM.",
                       MsgBoxStyle.Exclamation, "BomPushAddIn")
                Return
            End If

            Dim extractor As New BomExtractor()
            Dim items As List(Of BomLineItem) = extractor.TraverseBom(oDoc)
            Dim deduped As List(Of BomLineItem) = extractor.Dedup(items)

            ' Derive output filenames from the actual assembly's document
            ' name, not a fixed constant — otherwise every push overwrites
            ' the same file and the quote number JL Check derives from the
            ' filename never reflects which assembly was actually pushed.
            Dim assemblyBaseName As String = System.IO.Path.GetFileNameWithoutExtension(oDoc.FullFileName)
            Dim csvPath As String = System.IO.Path.Combine(OutputDirectory, assemblyBaseName & ".csv")
            Dim jsonPath As String = System.IO.Path.Combine(OutputDirectory, assemblyBaseName & ".json")

            System.IO.File.WriteAllText(csvPath, extractor.ToCsv(deduped))
            System.IO.File.WriteAllText(jsonPath, extractor.ToJson(deduped))

            LaunchJlCheck(jsonPath)

        Catch ex As Exception
            MsgBox("BOM extraction failed: " & ex.Message, MsgBoxStyle.Critical, "BomPushAddIn")
        End Try

    End Sub

    ''' <summary>
    ''' Launches JL Check (the JobBOSS material review tool) pointed at
    ''' the freshly-exported JSON, so it opens with the BOM already loaded
    ''' instead of requiring a manual "Load BOM JSON..." click.
    ''' </summary>
    Private Sub LaunchJlCheck(jsonPath As String)

        Const JlCheckPythonExe As String = "C:\Users\lstrain\source\jl_check\venv\Scripts\pythonw.exe"
        Const JlCheckMainScript As String = "C:\Users\lstrain\source\jl_check\main.py"

        Try
            Dim psi As New System.Diagnostics.ProcessStartInfo With {
                .FileName = JlCheckPythonExe,
                .Arguments = $"""{JlCheckMainScript}"" ""{jsonPath}""",
                .UseShellExecute = False
            }
            System.Diagnostics.Process.Start(psi)
        Catch ex As Exception
            MsgBox("Failed to launch JL Check: " & ex.Message, MsgBoxStyle.Exclamation, "BomPushAddIn")
        End Try

    End Sub

    ''' <summary>
    ''' Called by Inventor when the add-in unloads. Releases the Application
    ''' reference and prompts a GC pass — standard COM-interop hygiene.
    ''' </summary>
    Public Sub Deactivate() Implements Inventor.ApplicationAddInServer.Deactivate
        m_oButtonDef = Nothing
        m_inventorApplication = Nothing
        GC.Collect()
        GC.WaitForPendingFinalizers()
    End Sub

    ''' <summary>
    ''' Legacy pre-ribbon hook required by the interface; intentionally empty.
    ''' </summary>
    Public Sub ExecuteCommand(ByVal commandID As Integer) Implements Inventor.ApplicationAddInServer.ExecuteCommand
    End Sub

    ''' <summary>
    ''' Exposes an automation object for external callers; not used.
    ''' </summary>
    Public ReadOnly Property Automation() As Object Implements Inventor.ApplicationAddInServer.Automation
        Get
            Return Nothing
        End Get
    End Property

End Class
