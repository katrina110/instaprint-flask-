# instaprint-flask-
1. install python if you haven't install yet
1.1 go to your working dir 
2. install flask
    pip install flask
3. execute the command 
    python app.py    


 img  <image src="{{ url_for('static', filename= 'ricci.jpg')}}">
 css: <link rel="stylesheet" type="text/css" href="{{ url_for('static', filename= 'style.css')}}">


 in app.py every page(.html) app route: 
@app.route('/profile')
def profile():
    return render_template('profile.html')
