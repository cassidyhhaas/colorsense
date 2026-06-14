# API reference

The top-level `colorsense` package is the **canonical public API**: everything below is
re-exported there, so `from colorsense import ...` is the supported import path. Anything
not listed here is internal and free to change between releases.

## Entry point

::: colorsense.analyze

::: colorsense.LIGHT_AND_DARK

::: colorsense.DEFAULT_VIEWPORT

## Result & contracts

::: colorsense.AnalysisResult

::: colorsense.RunMetadata

::: colorsense.ThemePalette

::: colorsense.ColorUsage

::: colorsense.Usage

::: colorsense.UsagePalette

::: colorsense.UsageEntry

::: colorsense.Composition

::: colorsense.PaletteCandidate

::: colorsense.DivergenceItem

::: colorsense.DesignToken

::: colorsense.Color

::: colorsense.Viewport

### Enums

::: colorsense.Theme

::: colorsense.UsageRole

::: colorsense.PropertyFamily

::: colorsense.family_of

::: colorsense.PaletteRole

::: colorsense.ComponentType

::: colorsense.TokenSemanticRole

## Configuration

::: colorsense.Config

::: colorsense.load_default_config

::: colorsense.load_config

## Fetch policy & networking

::: colorsense.PolitenessPolicy

::: colorsense.block_private_networks

::: colorsense.RequestFilter

## Errors

::: colorsense.RenderError

::: colorsense.RobotsDisallowedError

::: colorsense.UnsupportedSchemeError

::: colorsense.AnalysisTimeoutError
