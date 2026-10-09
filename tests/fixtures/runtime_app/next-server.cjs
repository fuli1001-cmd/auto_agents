const http=require('node:http');
const next=require('next');
const port=Number(process.argv[2]);
const app=next({dev:true,hostname:'127.0.0.1',port});
app.prepare().then(()=>{
 const server=http.createServer(app.getRequestHandler());
 server.listen(port,'127.0.0.1');
 process.on('SIGTERM',()=>{server.close();app.close().then(()=>process.exit(0))});
}).catch(e=>{console.error(e);process.exit(1)});
