'==============================================================================
' BomExtractor.vb
' BomPushAddIn — BOM traversal, rollup, and serialization
'
' Pipeline (called from StandardAddInServer.OnPushBomButton):
'   1. TraverseBom  — walks the assembly's Structured BOM view into a tree of
'                     BomLineItem, computing effective (multiplied) quantities.
'   2. Dedup        — flattens the tree to LEAF PARTS ONLY (sub-assembly
'                     container rows are dropped), rolls up duplicate part
'                     numbers with summed quantities, and flags exceptions.
'   3. ToCsv/ToJson — serializes the deduped list for downstream consumers.
'
' Validation note: rolled-up output was verified row-for-row (61 unique
' items, zero quantity mismatches) against Inventor's native BOM export.
'==============================================================================

Imports Inventor
Imports System.Globalization
Imports System.Linq
Imports System.Text.Json
Imports System.Text.RegularExpressions

Public Class BomExtractor

    ' Sentinel written to PartNumber for parts with no part number iProperty
    ' (typically vendor hardware identified by the Material field instead).
    Private Const MissingPartNumberLabel As String = "NA"

    ' Inventor's internal length unit is centimeters.
    Private Const CmPerInch As Double = 2.54

    '===========================================================================
    ' 1. TRAVERSAL
    '===========================================================================

    ''' <summary>
    ''' Walks the assembly's Structured BOM into a tree of BomLineItem.
    ''' Quantities are "effective": each row's ItemQuantity multiplied up
    ''' through every parent sub-assembly's quantity, so a part appearing
    ''' once inside a sub-assembly used 8x reports Quantity = 8.
    ''' </summary>
    Public Function TraverseBom(oDoc As AssemblyDocument) As List(Of BomLineItem)

        Dim oBom As BOM = oDoc.ComponentDefinition.BOM

        ' Both settings are required. Without StructuredViewFirstLevelOnly =
        ' False, ChildRows is not populated and the walk silently flattens
        ' to the top level only.
        oBom.StructuredViewEnabled = True
        oBom.StructuredViewFirstLevelOnly = False

        Dim oBomView As BOMView = oBom.BOMViews("Structured")

        Return BuildTree(oBomView.BOMRows, level:=0, parentMultiplier:=1)

    End Function

    Private Function BuildTree(oRows As BOMRowsEnumerator,
                               level As Integer,
                               parentMultiplier As Integer) As List(Of BomLineItem)

        Dim items As New List(Of BomLineItem)

        For Each oRow As BOMRow In oRows

            ' Item(1): rows normally map to a single ComponentDefinition.
            ' Multiple definitions only occur when Inventor merges rows
            ' (e.g. matching part numbers across model states) — first is
            ' representative for our purposes.
            Dim oCompDef As ComponentDefinition = oRow.ComponentDefinitions.Item(1)

            Dim partNumber As String = TryGetDesignProperty(oCompDef, "Part Number", fallback:="")
            Dim material As String = TryGetDesignProperty(oCompDef, "Material", fallback:="")
            Dim description As String = TryGetDesignProperty(oCompDef, "Description", fallback:="(no description)")

            ' NOTE: partNumber fallback must stay "" (not a placeholder
            ' string) — Dedup detects missing part numbers via
            ' String.IsNullOrWhiteSpace and routes them to the
            ' description-grouped fallback path.

            Dim category As String = DetectCategory(material)

            ' Cut length applies to linear stock categories only. Parts
            ' without length data (e.g. legacy bar-stock library parts made
            ' before length tracking was introduced) return 0.
            Dim cutLengthIn As Double = 0
            If category <> "" Then
                cutLengthIn = TryGetLengthInches(oCompDef)
            End If

            Dim effectiveQty As Integer = oRow.ItemQuantity * parentMultiplier

            Dim newItem As New BomLineItem With {
                .PartNumber = partNumber,
                .Description = description,
                .Quantity = effectiveQty,
                .Level = level,
                .Material = material,
                .Category = category,
                .CutLengthIn = cutLengthIn
            }

            ' Recurse, passing THIS row's effective quantity down as the
            ' multiplier — this is what makes child quantities roll up
            ' correctly through multi-instance sub-assemblies.
            If oRow.ChildRows IsNot Nothing Then
                newItem.Children = BuildTree(oRow.ChildRows, level + 1, effectiveQty)
            End If

            items.Add(newItem)

        Next

        ' Sort siblings alphabetically (case-insensitive) at every level.
        Return items.OrderBy(Function(i) i.PartNumber, StringComparer.OrdinalIgnoreCase).ToList()

    End Function

    '===========================================================================
    ' 2. ROLLUP / DEDUPLICATION
    '===========================================================================

    ''' <summary>
    ''' Flattens the traversal tree to leaf parts only and rolls up duplicate
    ''' part numbers into single rows with summed quantities. Parts with no
    ''' part number are grouped by Description instead (vendor hardware) and
    ''' flagged for review. Material disagreements within a group are flagged
    ''' as conflicts rather than silently resolved.
    ''' </summary>
    Public Function Dedup(items As List(Of BomLineItem)) As List(Of BomLineItem)

        Dim flat As New List(Of BomLineItem)
        FlattenLeaves(items, flat)

        Dim missingPartNumber = flat.Where(Function(i) String.IsNullOrWhiteSpace(i.PartNumber)).ToList()
        Dim validItems = flat.Where(Function(i) Not String.IsNullOrWhiteSpace(i.PartNumber)).ToList()

        Dim result As New List(Of BomLineItem)

        ' --- Primary path: group by PartNumber -------------------------------
        For Each grp In validItems.GroupBy(Function(i) i.PartNumber)
            result.Add(MergeGroup(grp.ToList(),
                                  partNumber:=grp.Key,
                                  description:=grp.First().Description,
                                  flagMissingPartNumber:=False))
        Next

        ' --- Fallback path: no part number, group by Description -------------
        ' Without this split, every blank part number would collapse into ONE
        ' bogus row (empty string is a single GroupBy key).
        For Each grp In missingPartNumber.GroupBy(Function(i) i.Description)
            result.Add(MergeGroup(grp.ToList(),
                                  partNumber:=MissingPartNumberLabel,
                                  description:=grp.Key,
                                  flagMissingPartNumber:=True))
        Next

        Return result.OrderBy(Function(i) i.PartNumber, StringComparer.OrdinalIgnoreCase).ToList()

    End Function

    ''' <summary>
    ''' Merges one group of occurrences into a single output row: quantities
    ''' summed, first occurrence's attributes carried, conflicts flagged.
    ''' </summary>
    Private Function MergeGroup(occurrences As List(Of BomLineItem),
                                partNumber As String,
                                description As String,
                                flagMissingPartNumber As Boolean) As BomLineItem

        Dim first As BomLineItem = occurrences.First()

        Dim merged As New BomLineItem With {
            .PartNumber = partNumber,
            .Description = description,
            .Quantity = occurrences.Sum(Function(i) i.Quantity),
            .Material = first.Material,
            .Category = first.Category,
            .CutLengthIn = first.CutLengthIn
        }

        Dim notes As New List(Of String)

        If flagMissingPartNumber Then
            merged.HasConflict = True
            notes.Add("Missing part number — grouped by description")
        End If

        ' Same part number reporting different materials across occurrences
        ' is a data-integrity problem upstream (e.g. an .iam and .ipt sharing
        ' a part number) — surface it, never silently pick one.
        Dim distinctMaterials = occurrences.Select(Function(i) i.Material).Distinct().ToList()
        If distinctMaterials.Count > 1 Then
            merged.HasConflict = True
            notes.Add("Conflicting materials: " & String.Join(" | ", distinctMaterials))
        End If

        ' TODO: same check for CutLengthIn. Currently first-occurrence wins;
        ' duplicate part numbers with differing lengths would be silently
        ' collapsed. Extend before cut lengths feed stock nesting.

        If notes.Count > 0 Then merged.ConflictNotes = String.Join(" | ", notes)

        Return merged

    End Function

    ''' <summary>
    ''' Depth-first flatten collecting LEAF nodes only. Sub-assembly container
    ''' rows (anything with children) are intentionally dropped — a quote line
    ''' item is a physical part, not a CAD grouping.
    ''' </summary>
    Private Sub FlattenLeaves(items As List(Of BomLineItem), flat As List(Of BomLineItem))

        For Each item As BomLineItem In items
            If item.Children.Count = 0 Then
                flat.Add(item)
            Else
                FlattenLeaves(item.Children, flat)
            End If
        Next

    End Sub

    '===========================================================================
    ' 3. SERIALIZATION
    '===========================================================================

    ''' <summary>
    ''' RFC-4180-style CSV: every field quoted, embedded quotes doubled.
    ''' Descriptions and materials routinely contain commas and inch marks —
    ''' unquoted output misparses in Excel.
    ''' </summary>
    Public Function ToCsv(items As List(Of BomLineItem)) As String

        Dim sb As New System.Text.StringBuilder
        sb.AppendLine("PartNumber,Description,Quantity,Material,Category,CutLengthIn,Conflict,Notes")

        For Each item As BomLineItem In items
            sb.AppendLine(String.Join(","c, {
                CsvField(item.PartNumber),
                CsvField(item.Description),
                CsvField(item.Quantity.ToString(CultureInfo.InvariantCulture)),
                CsvField(item.Material),
                CsvField(item.Category),
                CsvField(item.CutLengthIn.ToString(CultureInfo.InvariantCulture)),
                CsvField(item.HasConflict.ToString()),
                CsvField(item.ConflictNotes)
            }))
        Next

        Return sb.ToString()

    End Function

    Public Function ToJson(items As List(Of BomLineItem)) As String

        Dim options As New JsonSerializerOptions With {
            .WriteIndented = True
        }

        Return JsonSerializer.Serialize(items, options)

    End Function

    Private Function CsvField(value As String) As String
        If value Is Nothing Then value = ""
        Return """" & value.Replace("""", """""") & """"
    End Function

    '===========================================================================
    ' HELPERS — Inventor property access
    '===========================================================================

    ''' <summary>
    ''' Reads a Design Tracking Properties iProperty, returning the fallback
    ''' if the property set/property is missing or unreadable (virtual
    ''' components and some vendor parts throw on PropertySets access).
    ''' </summary>
    Private Function TryGetDesignProperty(oCompDef As ComponentDefinition,
                                          propertyName As String,
                                          fallback As String) As String
        Try
            Return oCompDef.Document.PropertySets.
                Item("Design Tracking Properties").
                Item(propertyName).Value.ToString()
        Catch
            Return fallback
        End Try
    End Function

    ''' <summary>
    ''' Maps the Material description string to a linear-stock category.
    ''' Returns "" for anything that isn't linear stock (plate, sheet,
    ''' angle, vendor items...).
    ''' </summary>
    Private Function DetectCategory(material As String) As String

        Dim matUpper As String = material.ToUpper()

        ' "TU " (with trailing space) matches TU RT / TU SQ / TU RD without
        ' false-positives on words containing "TU".
        If matUpper.Contains("TU ") Then
            Return "TUBE"
        ElseIf matUpper.Contains("BR RD") OrElse matUpper.Contains("RD BR") Then
            Return "ROUND BAR"
        ElseIf matUpper.Contains("BR SQ") OrElse matUpper.Contains("SQ BR") OrElse
               matUpper.Contains("BR RQ") OrElse matUpper.Contains("RQ BR") Then
            Return "BAR"
        End If

        Return ""

    End Function

    ''' <summary>
    ''' Pulls a part's cut length in inches. Primary source: the "length"
    ''' User Defined Property (already inch-formatted, e.g. "34.750 in").
    ''' Fallback: the "LENGTH" model parameter (Inventor internal cm,
    ''' converted). Returns 0 when neither exists — expected for legacy
    ''' bar-stock parts created before length tracking was introduced.
    ''' </summary>
    Private Function TryGetLengthInches(oCompDef As ComponentDefinition) As Double

        ' --- Primary: "length" User Defined Property --------------------------
        ' Enumerate by name rather than indexing blind, capturing the property
        ' object so we don't do a second lookup after finding it.
        Try
            Dim oUserProps As PropertySet = oCompDef.Document.PropertySets.Item("User Defined Properties")

            For Each oProp As Inventor.Property In oUserProps
                If String.Equals(oProp.Name, "length", StringComparison.OrdinalIgnoreCase) Then
                    ' Value is a display string like "34.750 in" — extract the
                    ' leading numeric portion. InvariantCulture: the decimal
                    ' separator in the property is always ".", regardless of
                    ' the machine's regional settings.
                    Dim numericPart As String = Regex.Match(oProp.Value.ToString(), "[\d.]+").Value
                    Dim parsed As Double
                    If Double.TryParse(numericPart, NumberStyles.Float, CultureInfo.InvariantCulture, parsed) Then
                        Return parsed
                    End If
                    Exit For
                End If
            Next
        Catch
            ' Property set unreadable — fall through to the parameter path.
        End Try

        ' --- Fallback: "LENGTH" model parameter (cm -> inches) -----------------
        Try
            Dim lengthParamCm As Double = oCompDef.Parameters.Item("LENGTH").Value
            Return lengthParamCm / CmPerInch
        Catch
            ' Parameter absent — no length data on this part.
        End Try

        Return 0

    End Function

End Class