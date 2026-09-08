/* Keystroke interpretation is pure so recording shortcuts can be tested independently. */
(function(root) {
  const defaults = Object.freeze({section:'Space',marker:'M',previous:'U',pause:'P',undo:'Mod+Z',cancel:'Escape',help:'?'});
  function normalize(event) {
    let key = event.key === ' ' ? 'Space' : event.key;
    if (key?.length === 1 && key !== '?') key = key.toUpperCase();
    if (event.metaKey || event.ctrlKey) key = 'Mod+' + key;
    else if (event.altKey) key = 'Alt+' + key;
    return key;
  }
  function isTyping(target) {
    return Boolean(target && (target.isContentEditable || ['INPUT','TEXTAREA','SELECT'].includes(target.tagName) || target.closest?.('[contenteditable="true"],[role="textbox"]')));
  }
  function command(event, settings = {}, favorites = []) {
    if (event.repeat || event.isComposing || isTyping(event.target)) return null;
    const key = normalize(event);
    const bindings = {...defaults,...settings};
    const action = Object.keys(bindings).find(name => bindings[name].toLowerCase() === String(key).toLowerCase());
    if (action) return {action};
    const index = favorites.findIndex((label,i) => String(label.shortcut ?? (i < 9 ? i+1 : '')).toLowerCase() === String(key).toLowerCase());
    return index < 0 ? null : {action:'label',label:favorites[index]};
  }
  const api = {defaults,normalize,isTyping,command};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.EEGShortcuts = api;
})(typeof window !== 'undefined' ? window : globalThis);
