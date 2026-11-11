window.__ModuleLoader__.load({
  id: '@local/dsh-paper-desk',
  factory(require) {
    const React = require('react')
    const panelId = 'paper-desk'
    const pages = [
      ['/dashboard', '工作台', 'Dashboard'],
      ['/discover', '发现', 'Discover'],
      ['/library', '资料库', 'Library'],
      ['/materials', '材料', 'Materials'],
      ['/monitors', '监控', 'Monitors'],
      ['/system', '系统', 'System'],
    ]
    const text = (zh, en) => /^en\b/i.test(document.documentElement.lang) ? en : zh
    const title = () => text('凝态文献台', 'CondMat Paper Desk')
    const colorTokens = [
      'base', 'veil', 'sidebar', 'layer1', 'layer2', 'layer3', 'field', 'ink', 'ink2', 'ink3',
      'line', 'line2', 'line3', 'accent', 'accent2', 'accent3', 'accent-soft', 'danger', 'success',
      'warn', 'brand', 'brand-ink', 'brand-hover', 'selection', 'scroll', 'grid', 'grid-major', 'highlight', 'shadow',
    ]
    const literalColor = /^(?:#[\da-f]{3,8}|transparent|(?:rgba?|hsla?|oklch|oklab|lab|lch)\([\d\s.,/%+-]+\))$/i

    // The visual-theme provider publishes the active ID and its resolved CSS
    // properties on <html>, including choices derived from the native system mode.
    function readVisualTheme() {
      const clear = { type: 'dsh-paper-desk:theme', theme: null }
      const html = document.documentElement
      const theme = html.dataset.arknightsTheme
      if (!theme || !/^[a-z][a-z0-9-]{0,63}$/.test(theme)) return clear
      const style = window.getComputedStyle(html)
      const scheme = style.colorScheme
      if (scheme !== 'light' && scheme !== 'dark') return clear
      const tokens = {}
      for (const key of colorTokens) {
        const value = style.getPropertyValue('--ark-' + key).trim()
        if (value.length > 100 || !literalColor.test(value) || !window.CSS.supports('color', value)) return clear
        tokens[key] = value
      }
      return { type: clear.type, theme, scheme, tokens }
    }

    function makeButton(label, click) {
      const button = document.createElement('button')
      button.type = 'button'
      button.textContent = label
      button.addEventListener('click', click)
      return button
    }

    // The Windows preload owns the first two menu buttons in an open shadow root.
    function attachMenu(bar, openPage) {
      const shadow = bar.getRootNode()
      const trigger = makeButton('', () => popup.hidden ? show(0) : hide(true))
      trigger.dataset.paperDeskMenu = ''
      trigger.setAttribute('role', 'menuitem')
      trigger.setAttribute('aria-haspopup', 'menu')
      trigger.setAttribute('aria-expanded', 'false')
      trigger.setAttribute('aria-controls', 'paper-desk-menu')
      trigger.tabIndex = -1
      // Keep the new button immediately after Edit, even with other extensions.
      bar.children[1].after(trigger)

      const popup = document.createElement('div')
      popup.id = 'paper-desk-menu'
      popup.setAttribute('role', 'menu')
      popup.hidden = true
      const heading = document.createElement('strong')
      heading.id = 'paper-desk-menu-title'
      popup.setAttribute('aria-labelledby', heading.id)
      popup.append(heading)
      let focusBeforeMenu
      trigger.addEventListener('pointerdown', () => {
        const focused = document.activeElement
        focusBeforeMenu = focused?.shadowRoot?.activeElement ?? focused
      })
      const entries = pages.map(([route]) => {
        const entry = makeButton('', () => {
          const returnFocus = focusBeforeMenu ?? trigger
          hide(true)
          openPage(route, returnFocus)
        })
        entry.setAttribute('role', 'menuitem')
        entry.tabIndex = -1
        popup.append(entry)
        return entry
      })
      const style = document.createElement('style')
      style.textContent = `
        #paper-desk-menu { position:fixed; min-width:170px; max-height:calc(100vh - 42px); overflow:auto;
          z-index:1101; padding:5px; border:1px solid var(--dsw-alias-border-l1,#8886); border-radius:8px;
          background:var(--dsw-alias-bg-layer-1,Canvas); color:var(--dsw-alias-label-primary,CanvasText);
          box-shadow:0 5px 22px #0003; }
        #paper-desk-menu strong { display:block; padding:6px 10px; font-size:12px; }
        #paper-desk-menu button { display:block; width:100%; text-align:start; }
        #paper-desk-menu button:focus { background:var(--dsw-alias-interactive-bg-hover,#8882); }
        [data-paper-desk-back] { margin-inline-start:8px; padding-inline:9px; height:24px; border-radius:5px;
          font-size:12px; color:var(--ark-ink2,var(--dsw-alias-label-primary,CanvasText));
          border:1px solid var(--ark-line2,var(--dsw-alias-border-l1,#8886)); -webkit-app-region:no-drag; }
        [data-paper-desk-back]:hover { background:var(--ark-accent-soft,var(--dsw-alias-interactive-bg-hover,#8882)); }
      `
      shadow.append(style, popup)
      const updateLabels = () => {
        trigger.textContent = text('文献', 'Literature')
        heading.textContent = title()
        entries.forEach((entry, index) => { entry.textContent = text(...pages[index].slice(1)) })
      }
      updateLabels()
      const locale = new MutationObserver(updateLabels)
      locale.observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] })

      function hide(focus) {
        popup.hidden = true
        focusBeforeMenu = null
        trigger.setAttribute('aria-expanded', 'false')
        if (focus) trigger.focus()
      }
      function show(index) {
        const bounds = trigger.getBoundingClientRect()
        popup.style.left = bounds.left + 'px'
        popup.style.top = bounds.bottom + 'px'
        popup.hidden = false
        trigger.setAttribute('aria-expanded', 'true')
        entries[index].focus()
      }
      const onBarKey = event => {
        const buttons = Array.from(bar.querySelectorAll('[role="menuitem"]'))
        const from = buttons.indexOf(event.target)
        if (from === -1) return
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          // The desktop's two-button keyboard handler would otherwise skip this entry.
          event.preventDefault()
          event.stopPropagation()
          const delta = event.key === 'ArrowLeft' ? -1 : 1
          const next = buttons[(from + delta + buttons.length) % buttons.length]
          buttons.forEach(button => { button.tabIndex = button === next ? 0 : -1 })
          hide(false)
          next.focus()
        } else if (event.target === trigger && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
          event.preventDefault()
          show(event.key === 'ArrowUp' ? entries.length - 1 : 0)
        }
      }
      const onPopupKey = event => {
        const index = entries.indexOf(event.target)
        const last = entries.length - 1
        if (event.key === 'Escape') { event.preventDefault(); hide(true); return }
        if (event.key === 'Tab') { hide(true); return }
        const next = { ArrowDown: (index + 1) % entries.length, ArrowUp: (index + last) % entries.length, Home: 0, End: last }[event.key]
        if (next !== undefined) { event.preventDefault(); entries[next].focus() }
      }
      const outside = event => {
        const path = event.composedPath()
        if (!popup.hidden && !path.includes(popup) && !path.includes(trigger)) hide(false)
      }
      const blur = () => hide(false)
      bar.addEventListener('keydown', onBarKey, true)
      popup.addEventListener('keydown', onPopupKey)
      document.addEventListener('pointerdown', outside, true)
      document.addEventListener('focusin', outside, true)
      window.addEventListener('blur', blur)
      return () => {
        locale.disconnect()
        bar.removeEventListener('keydown', onBarKey, true)
        document.removeEventListener('pointerdown', outside, true)
        document.removeEventListener('focusin', outside, true)
        window.removeEventListener('blur', blur)
        if (trigger.tabIndex === 0 && bar.children[0]) bar.children[0].tabIndex = 0
        trigger.remove()
        popup.remove()
        style.remove()
      }
    }

    function watchTitlebar(openPage) {
      let bar, detach, backButton, surface
      const placeBack = () => {
        if (!backButton) return
        if (bar) bar.after(backButton)
        else surface.prepend(backButton)
      }
      const update = () => {
        const next = document.querySelector('[data-windows-menu]')?.shadowRoot?.querySelector('[role=menubar]')
        if (next === bar) return
        detach?.()
        bar = next
        detach = next?.children.length >= 2 ? attachMenu(next, openPage) : undefined
        placeBack()
      }
      const observer = new MutationObserver(update)
      observer.observe(document.body, { childList: true })
      update()
      return {
        setBack(button, parent) {
          backButton?.remove()
          backButton = button
          surface = parent
          placeBack()
        },
        dispose() { observer.disconnect(); detach?.(); backButton?.remove() },
      }
    }

    // Preserve the AppFrame (including its draggable caption) and the layout
    // store. Hide its workspace columns and prevent keyboard/accessibility access.
    function suspendWorkspace() {
      const overlay = document.querySelector('[data-shell-overlay]')
      const frame = overlay?.parentElement ?? document.getElementById('root')
      if (!frame) return () => {}
      const marked = new Set()
      const suspend = () => {
        // Leave the original theme picker and its React event delegation in
        // place. Every other branch stays inert, including sibling overlays.
        const picker = overlay?.querySelector('.ark-dock')
        const path = new Set()
        for (let node = picker; node && node !== frame; node = node.parentElement) path.add(node)
        let container = frame
        while (container && container !== picker) {
          let next
          for (const child of container.children) {
            if (path.has(child)) {
              next = child
              if (marked.delete(child)) child.inert = false
            } else if (!child.inert) { child.inert = true; marked.add(child) }
          }
          container = next
        }
      }
      const observer = new MutationObserver(suspend)
      observer.observe(frame, { childList: true })
      if (overlay) observer.observe(overlay, { childList: true, subtree: true })
      frame.setAttribute('data-paper-desk-background', '')
      suspend()
      return () => {
        observer.disconnect()
        frame.removeAttribute('data-paper-desk-background')
        for (const element of marked) element.inert = false
      }
    }

    function mountPanel(currentRoute, back, caption) {
      const root = document.createElement('section')
      root.dataset.paperDesk = ''
      root.setAttribute('aria-label', title())
      root.tabIndex = -1
      const style = document.createElement('style')
      style.textContent = `
        body:has(> [data-paper-desk]) { overflow:hidden; }
        [data-paper-desk-background] > * { visibility:hidden !important; }
        [data-paper-desk-background] > [data-shell-overlay] { z-index:1095 !important; pointer-events:none !important; }
        [data-paper-desk-background] [data-shell-overlay] [inert],
        [data-paper-desk-background] [data-shell-overlay] [inert] * { visibility:hidden !important; }
        [data-paper-desk-background] [data-shell-overlay] .ark-dock { visibility:visible !important; opacity:1 !important;
          pointer-events:none !important; }
        [data-paper-desk-background] .ark-dock .ark-sidebar-toggle { display:none !important; }
        [data-paper-desk-background] .ark-dock .ark-panel { position:fixed; top:calc(var(--dsh-windows-titlebar-height,34px) + 4px);
          right:clamp(8px,calc((100vw - 430px) / 2),152px); max-width:calc(100vw - 16px); }
        html:not([data-windows-titlebar]) [data-paper-desk-background] .ark-dock { top:3px; right:8px; }
        @media (max-width:600px) { [data-paper-desk-background] .ark-tab-label, [data-paper-desk-background] .ark-tab-swatch { display:none; } }
        [data-paper-desk] { position:fixed; inset:var(--dsh-windows-titlebar-height,0px) 0 0; z-index:1090;
          display:flex; flex-direction:column; overflow:hidden; overscroll-behavior:none;
          font:13px/1.45 var(--dsw-font-family,system-ui,sans-serif); outline:none;
          color:var(--ark-ink,var(--dsw-alias-label-primary,CanvasText)); background:var(--ark-base,var(--dsw-alias-bg-base,Canvas)); }
        [data-paper-desk] [data-error] { color:var(--ark-danger,var(--dsw-alias-state-error-primary,CanvasText)); }
        [data-paper-desk] button { color:var(--ark-accent,var(--dsw-alias-label-primary,CanvasText)); font:inherit;
          background:var(--ark-layer1,var(--dsw-alias-bg-layer-1,Canvas)); border:1px solid var(--ark-line2,var(--dsw-alias-border-l1,#8886)); border-radius:6px;
          padding:3px 9px; min-height:27px; cursor:pointer; }
        [data-paper-desk] button:hover { background:var(--ark-accent-soft,var(--dsw-alias-interactive-bg-hover,#8882)); }
        [data-paper-desk] button:focus-visible { outline:2px solid var(--ark-accent,var(--dsw-alias-state-business-primary,Highlight)); outline-offset:2px; }
        [data-paper-desk] > [data-paper-desk-back] { align-self:flex-start; margin:4px 8px; font-size:12px; }
        [data-paper-desk] [hidden] { display:none !important; }
      `
      const backButton = makeButton(text('返回 DSH', 'Back to DSH'), back)
      backButton.dataset.paperDeskBack = ''
      const arrow = document.createElement('span')
      arrow.setAttribute('aria-hidden', 'true')
      arrow.textContent = '‹ '
      backButton.prepend(arrow)
      const status = document.createElement('div')
      status.setAttribute('role', 'status')
      status.style.cssText = 'padding:1em;overflow-wrap:anywhere'
      const retry = makeButton(text('重试', 'Retry'), start)
      retry.style.cssText = 'align-self:flex-start;margin:0 1em 1em'
      root.append(status, retry, style)
      document.body.append(root)
      caption.setBack(backButton, root)
      root.focus({ preventScroll: true })
      const resumeWorkspace = suspendWorkspace()
      let request, timer, handshake, frame, origin, projectId, ready = false
      let sentTheme, expectedTheme, focusTimer, revealTimer
      function syncTheme() {
        if (!ready || !frame?.contentWindow) return
        const message = readVisualTheme()
        const signature = JSON.stringify(message)
        if (signature === sentTheme) return
        sentTheme = signature
        expectedTheme = message.theme
        frame.contentWindow.postMessage(message, origin)
      }
      const themeObserver = new MutationObserver(syncTheme)
      themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-arknights-theme', 'style', 'class'] })
      themeObserver.observe(document.head, { childList: true, subtree: true, characterData: true })
      const contrast = window.matchMedia?.('(prefers-contrast: more)')
      contrast?.addEventListener('change', syncTheme)
      const onWindowBlur = () => {
        // Pointer events inside a cross-origin iframe do not reach the picker's
        // document listener. Toggle its original control without moving focus.
        clearTimeout(focusTimer)
        focusTimer = setTimeout(() => {
          if (frame && document.activeElement === frame)
            document.querySelector('[data-shell-overlay] .ark-tab[aria-expanded="true"]')?.click()
        }, 0)
      }
      window.addEventListener('blur', onWindowBlur)

      function release() {
        request?.abort()
        clearTimeout(timer)
        clearTimeout(revealTimer)
        clearInterval(handshake)
        frame?.remove()
        frame = null
        origin = null
        projectId = null
        ready = false
        sentTheme = undefined
        expectedTheme = undefined
      }
      function fail(message) {
        release()
        status.hidden = false
        status.setAttribute('data-error', '')
        status.textContent = message
        retry.hidden = false
      }
      function sendRoute() {
        frame?.contentWindow?.postMessage({ type: 'dsh-paper-desk:navigate', route: currentRoute(), projectId }, origin)
      }
      function reveal() {
        clearTimeout(revealTimer)
        if (!frame?.hidden || !ready) return
        status.hidden = true
        frame.hidden = false
        if (document.activeElement === root) frame.focus({ preventScroll: true })
      }
      function onMessage(event) {
        if (!frame || event.source !== frame.contentWindow || event.origin !== origin) return
        if (event.data?.type === 'dsh-paper-desk:theme-applied') {
          if (ready && event.data.theme === expectedTheme) reveal()
          return
        }
        if (event.data?.type !== 'dsh-paper-desk:ready' || !pages.some(([route]) => route === event.data.route)) return
        if (ready) { syncTheme(); return }
        ready = true
        clearTimeout(timer)
        clearInterval(handshake)
        // A cached older workbench can acknowledge navigation but not its theme.
        revealTimer = setTimeout(reveal, 400)
        syncTheme()
        // Supply the host-issued XID, including when the first deep link already matches.
        sendRoute()
      }
      async function start() {
        release()
        status.hidden = false
        status.removeAttribute('data-error')
        retry.hidden = true
        status.textContent = text('正在启动凝态文献台…', 'Starting CondMat Paper Desk…')
        request = new AbortController()
        const { signal } = request
        timer = setTimeout(() => fail(text('文献服务启动超时。', 'The paper service timed out.')), 130000)
        try {
          // A desktop profile can live under a URL prefix. Resolve like other DSH routes.
          const result = await fetch('paper-desk/open', {
            method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal,
          })
          const data = await result.json().catch(() => ({}))
          if (signal.aborted) return
          if (!result.ok) throw new Error(data?.error || 'HTTP ' + result.status)
          if (typeof data?.origin !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9]\d{0,4}$/.test(data.origin) ||
              Number(data.origin.split(':').pop()) > 65535 || !data.projectId) {
            throw new Error(text('服务返回的工作区地址无效。', 'The service returned an invalid workspace address.'))
          }
          clearTimeout(timer)
          origin = data.origin
          projectId = data.projectId
          frame = document.createElement('iframe')
          frame.title = title()
          frame.hidden = true
          frame.referrerPolicy = 'origin'
          frame.style.cssText = 'flex:1 1 0;min-height:0;width:100%;border:0'
          frame.src = origin + currentRoute()
          frame.addEventListener('load', () => {
            // A fresh frame document loses its installed CSS even at the same URL.
            sentTheme = undefined
            sendRoute()
            syncTheme()
          })
          status.textContent = text('正在载入文献页面…', 'Loading the paper workspace…')
          timer = setTimeout(() => fail(text('文献页面没有响应。', 'The paper workspace did not respond.')), 30000)
          // A custom desktop scheme can omit Referer. Exchange window messages until
          // the mounted child confirms its source, including for an unchanged route.
          handshake = setInterval(sendRoute, 750)
          root.append(frame)
        } catch (error) {
          if (!signal.aborted) fail(String(error.message || error))
        }
      }
      window.addEventListener('message', onMessage)
      start()
      return {
        navigate: sendRoute,
        dispose() {
          release()
          clearTimeout(focusTimer)
          themeObserver.disconnect()
          contrast?.removeEventListener('change', syncTheme)
          window.removeEventListener('blur', onWindowBlur)
          window.removeEventListener('message', onMessage)
          caption.setBack(null, null)
          root.remove()
          resumeWorkspace()
        },
      }
    }

    return {
      inject: ['slots', 'layout'],
      apply(ctx) {
        ctx.slots.inject('main', () => {
          const selection = ctx.layout.panelInfo
          let active = selection.getSnapshot().activePanelId
          let previous = null
          let route = pages[0][0]
          let panel, previousFocus
          const caption = watchTitlebar((next, returnFocus) => {
            route = next
            // Keep the current DSH React tree mounted. Switching its keyed main
            // slot would discard transient views such as an open right inspector.
            show(returnFocus)
          })
          function close(restoreFocus) {
            if (!panel) return
            panel.dispose()
            panel = undefined
            if (restoreFocus && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
            previousFocus = null
          }
          function show(returnFocus) {
            if (panel) { panel.navigate(); return }
            previousFocus = returnFocus
            panel = mountPanel(() => route, () => {
              close(true)
              // Only the panel-picker fallback changes DSH's selection.
              if (selection.getSnapshot().activePanelId === panelId) {
                try { ctx.layout.selectPanel(previous) } catch { ctx.layout.selectPanel(null) }
              }
            }, caption)
          }
          function Desk() {
            React.useLayoutEffect(() => {
              show(document.activeElement)
              return () => close(false)
            }, [])
            return null
          }
          const unsubscribe = selection.subscribe(() => {
            const next = selection.getSnapshot().activePanelId
            if (next !== active) {
              if (next === panelId) previous = active
              else close(false)
              active = next
            }
          })
          const remove = ctx.slots.register({ name: 'main', key: panelId, label: title }, Desk)
          return () => { unsubscribe(); close(false); caption.dispose(); remove() }
        })
      },
    }
  },
})
