'==============================================================================
' BomLineItem.vb
' BomPushAddIn — data model for a single BOM line
'
' Plain data class (no logic). One instance represents either:
'   - a node in the raw traversal tree produced by BomExtractor.TraverseBom
'     (Children populated, quantities are effective/multiplied), or
'   - a merged row produced by BomExtractor.Dedup (Children empty, Quantity
'     is the rolled-up total across all occurrences).
'
' Serialized directly to JSON via System.Text.Json — property names here
' become the JSON keys, so renaming a property is a breaking change for
' any downstream consumer (e.g. the Python/JobBOSS bridge).
'==============================================================================

Public Class BomLineItem

    '--- Identity -------------------------------------------------------------

    ''' <summary>Part Number iProperty (Design Tracking Properties). Set to
    ''' "NA" by Dedup for parts with no part number (vendor hardware).</summary>
    Public Property PartNumber As String

    ''' <summary>Description iProperty. Doubles as the fallback grouping key
    ''' in Dedup when PartNumber is missing.</summary>
    Public Property Description As String

    '--- Material / sourcing ---------------------------------------------------

    ''' <summary>Material iProperty. For vendor items this carries the vendor
    ''' stock number (e.g. "VENDOR 11-0087"); for raw stock it carries the
    ''' internal stock description (e.g. "SS PL .25 X 48 X 120 T304 2B 28-0001").</summary>
    Public Property Material As String

    ''' <summary>Stock category derived from the Material string:
    ''' "TUBE", "ROUND BAR", "BAR", or "" for everything else.</summary>
    Public Property Category As String

    ''' <summary>Make/Buy flag. RESERVED — not yet populated.</summary>
    Public Property MakeBuy As String

    ''' <summary>Mapped JobBOSS material ID. RESERVED — not yet populated;
    ''' will be filled by the part-number -> JobBOSS mapping step.</summary>
    Public Property StockNumber As String

    '--- Quantities ------------------------------------------------------------

    ''' <summary>Effective quantity: this row's ItemQuantity multiplied up
    ''' through every parent sub-assembly's quantity. After Dedup, the summed
    ''' total across all occurrences of the part number.</summary>
    Public Property Quantity As Integer

    '--- Cut / stock data -------------------------------------------------------

    ''' <summary>Cut length in inches, pulled for TUBE / ROUND BAR / BAR
    ''' categories from the "length" User Defined Property (already inches),
    ''' falling back to the "LENGTH" model parameter (cm, converted).
    ''' 0 when no length data exists on the part.</summary>
    Public Property CutLengthIn As Double

    ''' <summary>Raw cut length in Inventor internal units (cm).
    ''' RESERVED — not currently populated; CutLengthIn is authoritative.</summary>
    Public Property CutLengthCm As Double

    ''' <summary>Sheet-metal flat pattern area. RESERVED — not yet populated.</summary>
    Public Property FlatPatternArea As Double

    ''' <summary>Part mass. RESERVED — not yet populated.</summary>
    Public Property Mass As Double

    '--- Tree / provenance -------------------------------------------------------

    ''' <summary>Depth in the BOM tree (0 = top level). Meaningful on the
    ''' traversal tree only; not meaningful after Dedup.</summary>
    Public Property Level As Integer

    ''' <summary>Part number of the immediate parent assembly.
    ''' RESERVED — not yet populated; would make conflict rows self-locating.</summary>
    Public Property ParentAssembly As String

    ''' <summary>Child rows (sub-assembly contents). Populated on the traversal
    ''' tree; always empty on Dedup output, which emits leaf parts only.</summary>
    Public Property Children As New List(Of BomLineItem)

    '--- Exception flags ----------------------------------------------------------

    ''' <summary>True when this row needs human review before import:
    ''' conflicting material values across occurrences, or missing part number.</summary>
    Public Property HasConflict As Boolean

    ''' <summary>Human-readable explanation of why HasConflict is set.</summary>
    Public Property ConflictNotes As String

End Class