export const dynamic='force-dynamic';
async function forward(request:Request){
 const name=new URL(request.url).pathname.slice(4);
 const response=await fetch(process.env.AGT_API_BASE+name,{method:request.method,cache:'no-store'});
 return new Response(await response.text(),{status:response.status,headers:{'content-type':'application/json'}})
}
export const GET=forward;
export const POST=forward;
