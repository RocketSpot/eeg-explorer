const {app,BrowserWindow,dialog,shell}=require('electron');
const {spawn}=require('node:child_process');const path=require('node:path');let child,win,closing=false;
app.setName('EEG Explorer');app.setPath('userData',path.join(app.getPath('appData'),'Zone EEG Explorer Desktop'));
if(!app.requestSingleInstanceLock()){app.quit();}else{
app.on('second-instance',()=>{if(win){win.show();win.focus();}});
app.whenReady().then(()=>{
const python=process.env.EEG_EXPLORER_PYTHON||path.join(__dirname,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
child=spawn(python,['-m','explorer.server','--port','0'],{cwd:__dirname,env:{...process.env,PYTHONUNBUFFERED:'1'},stdio:['ignore','pipe','pipe']});
let buffer='',errors='';child.stderr.on('data',d=>{errors=(errors+d).slice(-5000)});
child.on('error',e=>dialog.showErrorBox('EEG Explorer could not start',e.message+'\nRun tools/setup.sh first.'));
child.stdout.on('data',d=>{buffer+=d;let i;while((i=buffer.indexOf('\n'))>=0){let line=buffer.slice(0,i);buffer=buffer.slice(i+1);let data;try{data=JSON.parse(line)}catch{continue}if(!data.ready)continue;
win=new BrowserWindow({width:1500,height:980,minWidth:1060,minHeight:700,title:'EEG Explorer',backgroundColor:'#10151f',webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true}});
win.webContents.setWindowOpenHandler(({url})=>{if(url.startsWith(data.url+'/api/export?'))win.webContents.downloadURL(url);return {action:'deny'};});
win.webContents.on('will-navigate',(event,url)=>{if(!url.startsWith(data.url+'/'))event.preventDefault();});win.loadURL(data.url);
}});
child.on('exit',code=>{if(!closing&&code)dialog.showErrorBox('EEG Explorer stopped',errors||'See the local session library for recovered recordings.');if(!closing)app.quit();});
});
app.on('window-all-closed',()=>app.quit());app.on('before-quit',event=>{if(child&&!closing){event.preventDefault();closing=true;child.once('exit',()=>app.quit());child.kill('SIGTERM');setTimeout(()=>app.exit(0),10000).unref();}});
}
