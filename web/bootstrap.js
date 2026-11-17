
    // Read preference before painting; this browser's choice survives server port changes.
    try{const saved=document.cookie.split('; ').find(item=>item.startsWith('aioa-theme='))?.split('=')[1];document.documentElement.dataset.theme=saved==='dark'||saved==='light'?saved:'dark'}catch{document.documentElement.dataset.theme='dark'}
  