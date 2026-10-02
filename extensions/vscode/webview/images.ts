import type { ImageAttachment } from '../src/protocol';

// 给消息输入框安装图片选择、粘贴、拖放和可移除的预览。
export function installImages(input: HTMLTextAreaElement, changed: () => void) {
  const picker = document.getElementById('image-picker') as HTMLInputElement;
  const preview = document.getElementById('image-attachments')!;
  const error = document.getElementById('image-error')!;
  const upload = document.getElementById('upload-images') as HTMLButtonElement;
  let images: ImageAttachment[] = [];
  let pending = 0;
  let generation = 0;
  const render = () => {
    preview.replaceChildren(); preview.hidden = !images.length;
    images.forEach((image, index) => {
      const item = document.createElement('div'); item.className = 'image-attachment';
      const img = document.createElement('img'); img.src = `data:${image.media_type};base64,${image.data}`; img.alt = image.name;
      const remove = document.createElement('button'); remove.textContent = '×'; remove.setAttribute('aria-label', `移除 ${image.name}`);
      remove.disabled = input.disabled;
      remove.onclick = () => { images.splice(index, 1); render(); changed(); };
      item.title = image.name; item.append(img, remove); preview.append(item);
    });
  };
  const add = async (files: File[]) => {
    if (input.disabled) return;
    const current = generation;
    error.hidden = true;
    for (const file of files) {
      try {
        if (images.length + pending >= 4) throw new Error('每条消息最多上传 4 张图片');
        if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.type)) throw new Error('请使用 PNG、JPEG、GIF 或 WebP 图片');
        if (file.size > 3 * 1024 * 1024) throw new Error('每张图片不能超过 3 MiB，请缩小后上传');
        pending++; changed();
        let data: string;
        try {
          data = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]);
            reader.onerror = () => reject(new Error('读取图片失败')); reader.readAsDataURL(file);
          });
        } finally { pending--; changed(); }
        if (current !== generation) return;
        images.push({ name: file.name.slice(0, 255), media_type: file.type as ImageAttachment['media_type'], data });
      } catch (reason) {
        error.textContent = reason instanceof Error ? reason.message : String(reason); error.hidden = false;
      }
      render(); changed();
    }
  };
  upload.onclick = () => { if (!input.disabled) picker.click(); };
  picker.onchange = () => { void add(Array.from(picker.files ?? [])); picker.value = ''; };
  input.addEventListener('paste', event => {
    const files = Array.from(event.clipboardData?.items ?? []).filter(item => item.kind === 'file').map(item => item.getAsFile()).filter((file): file is File => !!file);
    if (files.length && !input.disabled) { event.preventDefault(); void add(files); }
  });
  const shell = input.closest('.composer-shell')!;
  shell.addEventListener('dragover', event => {
    const drag = event as DragEvent;
    if (!input.disabled && drag.dataTransfer?.types.includes('Files')) drag.preventDefault();
  });
  shell.addEventListener('drop', event => {
    const drag = event as DragEvent;
    if (drag.dataTransfer?.files.length) { drag.preventDefault(); void add(Array.from(drag.dataTransfer.files)); }
  });
  return {
    get: () => images,
    loading: () => pending > 0,
    clear: () => { generation++; images = []; error.hidden = true; render(); changed(); },
    restore: (value: ImageAttachment[]) => { generation++; images = [...value]; render(); changed(); },
    render: () => { upload.disabled = input.disabled; render(); },
  };
}
