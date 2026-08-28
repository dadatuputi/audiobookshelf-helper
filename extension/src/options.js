const F=["absUrl","apiKey","devicePath","subdir","folderTemplate","localRoot","sourceMode"];
const C=["renameM4b"];
browser.storage.local.get({absUrl:"",apiKey:"",devicePath:"",subdir:"AUDIOBOOKS",
  folderTemplate:"{author} - {title}",localRoot:"",sourceMode:"auto",renameM4b:true}).then(d=>{
  F.forEach(k=>{if(document.getElementById(k))document.getElementById(k).value=d[k]||"";});
  C.forEach(k=>{document.getElementById(k).checked=!!d[k];});
});
document.getElementById("save").addEventListener("click",async()=>{
  const o={};F.forEach(k=>o[k]=document.getElementById(k).value.trim());
  C.forEach(k=>o[k]=document.getElementById(k).checked);
  await browser.storage.local.set(o);
  const m=document.getElementById("msg");m.textContent="saved";setTimeout(()=>m.textContent="",1500);
});
