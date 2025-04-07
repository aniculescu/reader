import * as fs from 'fs';
import * as cheerio from 'cheerio';

function addTmplHeader(metadata){

}

async function scrapeHTML(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const html = await response.text();
    const $ = cheerio.load(html);
    let data;

    const lectureContent = $(".articleBox");
    //remove navigation

    $(lectureContent).find(".lectureNav").remove();
    //remove headers
    $(lectureContent).find("h2").remove();
    $(lectureContent).find("h3").remove();
    $(lectureContent).find("p.date").remove();
    $(lectureContent).html().trim();
    //add metadata
    $(lectureContent).prepend("---\nlayout: lecture-layout\n\ntitle:");
    const lectureImages = $(lectureContent).find("img");
    if(lectureImages.length > 0){

      lectureImages.each((i,e)=>{
        const imgSrc = $(e).attr("src");
        const imgFile = imgSrc.split("/").pop();
        const imgUrl = "https://rsarchive.org/Lectures/GA134/English/RSPC1947/"+imgSrc;
        const filepath = "lectures/images/";
        const filename = "ga134-lecture6-"+imgFile;

        // console.log("<img src='"+imgSrc+"'>");
        console.log("Downloading: " +imgUrl);
        downloadImage(imgUrl, filepath, filename)
          .then(() => {
            console.log('Image downloaded successfully!');
          })
          .catch(err => {
            console.error('Error downloading image:', err);
          });

        $(e).attr("src", filepath+filename);
        // console.log("new src: "+$(e).attr("src"));
        // console.log("img el:");
        // console.dir($(`img[src='${imgSrc}']`).prop('outerHTML'));
        
      });   
    }
    data = $(lectureContent).html().trim();
    // console.log(data.indexOf("ga134-lecture6"));
    return data;

  } catch (error) {
    console.error("Error during scraping:", error);
    return null;
  }
}

async function createFile(n,c) {
  try {
    const content = c;
    await fs.writeFileSync(n, content);
  } catch (err) {
    console.log(err);
  }
}

async function downloadImage(url, filepath,filename) {
  const response = await fetch(url);
  const arrayBuffer = await response.arrayBuffer();
  const buffer = Buffer.from(arrayBuffer);

  fs.mkdir(filepath, { recursive: true }, (err) => {
    if (err) throw err;
    createFile(filepath+filename, buffer);
    
  });
 
}

// Usage example:
const url = 'https://rsarchive.org/Lectures/GA134/English/RSPC1947/19120101p01.html';
scrapeHTML(url).then(data => {
  if (data) {
    // console.log("Scraped data:", data);
    // console.log(data);
      createFile("ga-134-lecture-6.html",data);


  }
});