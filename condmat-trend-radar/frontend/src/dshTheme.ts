/** Copy the installed DSH theme's published color tokens across the iframe boundary. */
const colors = [
  'base', 'veil', 'sidebar', 'layer1', 'layer2', 'layer3', 'field', 'ink', 'ink2', 'ink3',
  'line', 'line2', 'line3', 'accent', 'accent2', 'accent3', 'accent-soft', 'danger', 'success',
  'warn', 'brand', 'brand-ink', 'brand-hover', 'selection', 'scroll', 'grid', 'grid-major',
  'highlight', 'shadow',
] as const;

export function applyDshTheme(theme: unknown, scheme: unknown, tokens: unknown) {
  const root = document.documentElement;
  if (theme === null) {
    if (!root.hasAttribute('data-dsh-theme')) return true;
    delete root.dataset.dshTheme;
    delete root.dataset.dshScheme;
    colors.forEach(key => root.style.removeProperty('--paper-host-' + key));
  } else {
    if (typeof theme !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(theme) ||
        (scheme !== 'light' && scheme !== 'dark') || !tokens || typeof tokens !== 'object') return false;
    const values = tokens as Record<string, unknown>;
    const updates: Array<[string, string]> = [];
    for (const key of colors) {
      const value = values[key];
      // The host sends colors only. Background images, URLs and CSS-wide keywords are outside this contract.
      if (typeof value !== 'string' || value.length > 100 ||
          !/^(?:#[\da-f]{3,8}|transparent|(?:rgba?|hsla?|oklch|oklab|lab|lch)\([\d\s.,/%+-]+\))$/i.test(value) ||
          !CSS.supports('color', value)) return false;
      const property = '--paper-host-' + key;
      if (root.style.getPropertyValue(property) !== value) updates.push([property, value]);
    }
    if (!updates.length && root.dataset.dshTheme === theme && root.dataset.dshScheme === scheme) return true;
    for (const [property, value] of updates) root.style.setProperty(property, value);
    root.dataset.dshScheme = scheme;
    root.dataset.dshTheme = theme;
  }
  window.dispatchEvent(new Event('dsh-paper-desk:themechange'));
  return true;
}
