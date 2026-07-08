from ..logger import logger
from ..models import ProductData
from ..browser import click_tab
import re
import time

def fill_seo(page, data: ProductData):
    click_tab(page, "SEO")
    s = data.seo
    title = s.get("SEO Title", "")
    desc = s.get("SEO Description", "")
    keywords = s.get("SEO Keywords", "")
    logger.info("Filling SEO by confirmed visible field order:")
    logger.info("  1st field = SEO Title")
    logger.info("  2nd field = SEO Description")
    logger.info("  3rd field = SEO Keywords")

    res = page.evaluate(
        """({title, desc, keywords}) => {
            function isVisible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
            }
            function setValue(el, val) {
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                setter.call(el, val || '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }
            const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea')]
                .filter(isVisible)
                .filter(el => !el.disabled && !el.readOnly)
                .map(el => ({el, r: el.getBoundingClientRect(), ph: el.placeholder || ''}))
                .sort((a,b) => a.r.top - b.r.top || a.r.left - b.r.left);
            const details = fields.map((f, i) => ({index:i+1, tag:f.el.tagName, placeholder:f.ph, y:Math.round(f.r.top), oldValue:f.el.value||''}));
            if (fields.length >= 1) setValue(fields[0].el, title);
            if (fields.length >= 2) setValue(fields[1].el, desc);
            if (fields.length >= 3) setValue(fields[2].el, keywords);
            return {count: fields.length, details};
        }""",
        {"title": title, "desc": desc, "keywords": keywords},
    )
    for f in res.get("details", []):
        logger.info(f"  field #{f['index']}: {f['tag']} placeholder='{f['placeholder']}' y={f['y']}")
    logger.info("SEO fill done.")

