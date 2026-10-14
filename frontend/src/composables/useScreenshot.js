import { ref } from 'vue'
import { useLocale } from './useLocale.js'

export function useScreenshot({ graph, rootPersonName }) {
  const { prefName, prefDesc } = useLocale()
  const exporting = ref(false)

  async function exportScreenshot() {
    const cy = graph.cy.value
    if (!cy || exporting.value) return
    exporting.value = true
    try {
      const container = cy.container()
      if (!container) return
      const dpr = window.devicePixelRatio || 1
      const maxViewportDim = Math.max(container.clientWidth, container.clientHeight) || 800
      // baseScale targets ~4800px on the longer CSS-pixel axis.
      // Multiply by dpr (capped at 2) so Retina/high-DPI screens get physical-pixel accuracy.
      // Total capped at 10 to avoid runaway canvas sizes on 3× mobile displays.
      const baseScale = Math.max(2, Math.min(6, Math.round(4800 / maxViewportDim)))
      const scale = Math.min(10, baseScale * Math.min(dpr, 2))

      const dataUrl = cy.png({
        output: 'base64uri',
        bg: '#f2ede3',
        scale,
        full: true,
        maxWidth: 14000,
        maxHeight: 14000,
      })

      const img = await new Promise((resolve, reject) => {
        const i = new Image()
        i.onload = () => resolve(i)
        i.onerror = reject
        i.src = dataUrl
      })

      const border = Math.round(img.width * 0.04)
      const canvas = document.createElement('canvas')
      canvas.width = img.width + border * 2
      canvas.height = img.height + border * 2
      const ctx = canvas.getContext('2d')

      ctx.fillStyle = '#f2ede3'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.drawImage(img, border, border)

      const pad = Math.max(14, canvas.width * 0.012)
      const fontSize = Math.max(16, Math.floor(canvas.height * 0.0216))

      // ── Legend (top-right) — read from DOM ──
      drawLegendFromDOM(ctx, canvas.width, pad, fontSize)

      // ── Subject info card (bottom-right) ──
      const rootId = graph.rootNodeId?.value
      const rootNode = rootId ? graph.cy.value?.$id(rootId) : null
      drawInfoCard(ctx, canvas.width, canvas.height, pad, fontSize, rootNode)

      // ── Watermark pill (bottom-left) ──
      ctx.font = `600 ${fontSize}px Inter, DM Sans, sans-serif`
      const text = 'humansmap.com'
      const textW = ctx.measureText(text).width
      const pillPadX = fontSize * 0.7
      const pillPadY = fontSize * 0.45
      const pillW = textW + pillPadX * 2
      const pillH = fontSize + pillPadY * 2
      const pillX = pad
      const pillY = canvas.height - pad - pillH
      const r = pillH / 2

      ctx.fillStyle = 'rgba(28, 21, 16, 0.72)'
      ctx.beginPath()
      ctx.moveTo(pillX + r, pillY)
      ctx.lineTo(pillX + pillW - r, pillY)
      ctx.arcTo(pillX + pillW, pillY, pillX + pillW, pillY + r, r)
      ctx.lineTo(pillX + pillW, pillY + pillH - r)
      ctx.arcTo(pillX + pillW, pillY + pillH, pillX + pillW - r, pillY + pillH, r)
      ctx.lineTo(pillX + r, pillY + pillH)
      ctx.arcTo(pillX, pillY + pillH, pillX, pillY + pillH - r, r)
      ctx.lineTo(pillX, pillY + r)
      ctx.arcTo(pillX, pillY, pillX + r, pillY, r)
      ctx.closePath()
      ctx.fill()

      ctx.fillStyle = 'rgba(249, 248, 244, 0.92)'
      ctx.textAlign = 'left'
      ctx.textBaseline = 'top'
      ctx.fillText(text, pillX + pillPadX, pillY + pillPadY)

      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
      if (!blob) throw new Error('canvas.toBlob returned null')

      const displayName = prefName(rootNode?.data() || {}) || rootNode?.data('label') || rootPersonName.value
      const slug = displayName
        ? displayName.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') + '_humansmap'
        : 'humansmap'
      const filename = slug + '.png'

      let shared = false
      if (navigator.share && navigator.canShare?.({ files: [new File([blob], filename, { type: 'image/png' })] })) {
        try {
          const file = new File([blob], filename, { type: 'image/png' })
          await navigator.share({ files: [file], title: 'humansmap.com' })
          shared = true
        } catch (shareErr) {
          if (shareErr?.name === 'AbortError') return
          // NotAllowedError (gesture context expired) or other — fall through to download
        }
      }
      if (!shared) {
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        setTimeout(() => URL.revokeObjectURL(url), 10000)
      }
    } catch (e) {
      if (e?.name !== 'AbortError') console.error('Export failed', e)
    } finally {
      exporting.value = false
    }
  }

  function drawInfoCard(ctx, canvasW, canvasH, pad, fontSize, rootNode) {
    if (!rootNode || !rootNode.length) return

    const name = prefName(rootNode.data() || {}) || rootNode.data('label') || ''
    if (!name) return

    const entityType = rootNode.data('entityType') || 'person'
    const isPerson = entityType !== 'organization'

    // Build subtitle: description first, then fallback metadata
    let subtitle = prefDesc(rootNode.data() || '')
    if (!subtitle) {
      if (isPerson) {
        const birthDate = rootNode.data('birth_date')
        const occupations = rootNode.data('occupations')
        const birthYear = birthDate ? birthDate.slice(0, 4) : null
        const firstOcc = Array.isArray(occupations) && occupations.length ? occupations[0] : null
        if (birthYear && firstOcc) subtitle = `b. ${birthYear} · ${firstOcc}`
        else if (birthYear) subtitle = `b. ${birthYear}`
        else if (firstOcc) subtitle = firstOcc
      } else {
        const founded = rootNode.data('founded')
        if (founded) subtitle = `Founded ${String(founded).slice(0, 4)}`
      }
    }

    const nameSize = Math.round(fontSize * 0.95)
    const subSize = Math.round(fontSize * 0.72)
    const panelPadX = Math.round(nameSize * 1.1)
    const panelPadY = Math.round(nameSize * 0.8)
    const nameSubGap = Math.round(nameSize * 0.55)

    // Measure text to size the panel
    ctx.font = `700 ${nameSize}px Inter, DM Sans, sans-serif`
    const nameW = ctx.measureText(name).width
    ctx.font = `400 ${subSize}px Inter, DM Sans, sans-serif`
    const subW = subtitle ? ctx.measureText(subtitle).width : 0

    const maxTextW = Math.max(nameW, subW)
    const panelW = panelPadX * 2 + maxTextW
    const panelH = subtitle
      ? panelPadY * 2 + nameSize + nameSubGap + subSize
      : panelPadY * 2 + nameSize

    const x = canvasW - pad - panelW
    const y = canvasH - pad - panelH
    const r = Math.round(nameSize * 0.5)

    // Panel background
    ctx.fillStyle = 'rgba(249, 248, 244, 0.92)'
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.08)'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(x + r, y)
    ctx.lineTo(x + panelW - r, y)
    ctx.arcTo(x + panelW, y, x + panelW, y + r, r)
    ctx.lineTo(x + panelW, y + panelH - r)
    ctx.arcTo(x + panelW, y + panelH, x + panelW - r, y + panelH, r)
    ctx.lineTo(x + r, y + panelH)
    ctx.arcTo(x, y + panelH, x, y + panelH - r, r)
    ctx.lineTo(x, y + r)
    ctx.arcTo(x, y, x + r, y, r)
    ctx.closePath()
    ctx.fill()
    ctx.stroke()

    // Name
    ctx.fillStyle = 'rgba(28, 21, 16, 0.9)'
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.font = `700 ${nameSize}px Inter, DM Sans, sans-serif`
    ctx.fillText(name, x + panelPadX, y + panelPadY)

    // Subtitle
    if (subtitle) {
      ctx.fillStyle = 'rgba(28, 21, 16, 0.52)'
      ctx.font = `400 ${subSize}px Inter, DM Sans, sans-serif`
      ctx.fillText(subtitle, x + panelPadX, y + panelPadY + nameSize + nameSubGap)
    }
  }

  function drawLegendFromDOM(ctx, canvasW, pad, fontSize) {
    const legendEl = document.querySelector('.graph-legend')
    if (!legendEl) return

    const rows = legendEl.querySelectorAll('.legend-row')
    if (!rows.length) return

    const items = []
    for (const row of rows) {
      const swatch = row.querySelector('.node-swatch')
      const label = row.querySelector('.legend-label')
      if (!swatch || !label) continue

      const style = getComputedStyle(swatch)
      const color = style.backgroundColor || style.borderColor || '#8a7b6e'
      const borderColor = style.borderColor
      const boxShadow = style.boxShadow
      const borderRadius = style.borderRadius

      let shape = 'circle'
      if (borderRadius && parseInt(borderRadius) < 50) shape = 'rect'

      items.push({
        label: label.textContent.trim(),
        color,
        borderColor,
        boxShadow,
        shape,
      })
    }

    if (!items.length) return

    const labelSize = Math.round(fontSize * 0.8)
    const swatchSize = Math.round(labelSize * 1.0)
    const rowH = Math.round(labelSize * 2.0)
    const gap = Math.round(labelSize * 0.6)
    const panelPadX = Math.round(labelSize * 1.0)
    const panelPadY = Math.round(labelSize * 0.8)

    ctx.font = `500 ${labelSize}px Inter, DM Sans, sans-serif`
    const maxTextW = Math.max(...items.map(i => ctx.measureText(i.label).width))
    const panelW = panelPadX * 2 + swatchSize + gap + maxTextW
    const panelH = panelPadY * 2 + items.length * rowH - (rowH - swatchSize)

    const x = canvasW - pad - panelW
    const y = pad

    // Panel background
    const r = Math.round(labelSize * 0.5)
    ctx.fillStyle = 'rgba(249, 248, 244, 0.92)'
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.08)'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(x + r, y)
    ctx.lineTo(x + panelW - r, y)
    ctx.arcTo(x + panelW, y, x + panelW, y + r, r)
    ctx.lineTo(x + panelW, y + panelH - r)
    ctx.arcTo(x + panelW, y + panelH, x + panelW - r, y + panelH, r)
    ctx.lineTo(x + r, y + panelH)
    ctx.arcTo(x, y + panelH, x, y + panelH - r, r)
    ctx.lineTo(x, y + r)
    ctx.arcTo(x, y, x + r, y, r)
    ctx.closePath()
    ctx.fill()
    ctx.stroke()

    // Draw each row
    let rowY = y + panelPadY
    for (const item of items) {
      const sx = x + panelPadX
      const sy = rowY

      // Draw swatch
      if (item.boxShadow && item.boxShadow !== 'none') {
        // Root node style: circle with ring
        ctx.fillStyle = '#ece8de'
        ctx.beginPath()
        ctx.arc(sx + swatchSize / 2, sy + swatchSize / 2, swatchSize / 2, 0, Math.PI * 2)
        ctx.fill()
        ctx.strokeStyle = '#d4920a'
        ctx.lineWidth = Math.max(2, swatchSize * 0.15)
        ctx.stroke()
      } else if (item.borderColor && item.borderColor !== 'rgba(0, 0, 0, 0)') {
        // Bordered swatch (role/family items)
        ctx.fillStyle = 'rgba(236, 232, 222, 0.5)'
        ctx.strokeStyle = item.borderColor
        ctx.lineWidth = Math.max(2, swatchSize * 0.15)
        ctx.beginPath()
        ctx.arc(sx + swatchSize / 2, sy + swatchSize / 2, swatchSize / 2 - 1, 0, Math.PI * 2)
        ctx.fill()
        ctx.stroke()
      } else {
        // Filled swatch
        ctx.fillStyle = item.color
        ctx.beginPath()
        ctx.arc(sx + swatchSize / 2, sy + swatchSize / 2, swatchSize / 2, 0, Math.PI * 2)
        ctx.fill()
      }

      // Draw label
      ctx.fillStyle = 'rgba(28, 21, 16, 0.75)'
      ctx.textAlign = 'left'
      ctx.textBaseline = 'middle'
      ctx.font = `500 ${labelSize}px Inter, DM Sans, sans-serif`
      ctx.fillText(item.label, sx + swatchSize + gap, sy + swatchSize / 2)

      rowY += rowH
    }
  }

  return { exporting, exportScreenshot }
}
