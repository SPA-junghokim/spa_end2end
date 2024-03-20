
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import sys
import json
import os
from os import path as osp

def send_mail(result_line_list, model, server_number):
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    server = smtplib.SMTP(smtp_server, smtp_port)
    server.starttls()

    sender_email = "kimjh7669@gmail.com"
    sender_password = "glviltplcmfdvwvh"
    server.login(sender_email, sender_password)

    try:
        if result_line_list is not None:
            for result in result_line_list:
                if 'NuscMap_chamfer/divider_AP: ' in result:
                    divider_AP = round(float(result.split('NuscMap_chamfer/divider_AP: ')[1].split(', NuscMap_chamfer/ped_crossing_AP:')[0].strip()), 4)
                    ped_crossing_AP = round(float(result.split('NuscMap_chamfer/ped_crossing_AP: ')[1].split(', NuscMap_chamfer/boundary_AP')[0].strip()), 4)
                    boundary_AP = round(float(result.split('NuscMap_chamfer/boundary_AP: ')[1].split(', NuscMap_chamfer/m')[0].strip()), 4)
                    mAP = round(float(result.split('NuscMap_chamfer/mAP: ')[1].split(', NuscMap_chamfer')[0].strip()), 4)
            subject = f"MapTRv2_sh (in server {server_number}) - mAP: {mAP} - {model}"
            message = f"mAP: {mAP}\nboundary_AP: {boundary_AP}\ndivider_AP: {divider_AP}\nped_crossing_AP: {ped_crossing_AP}"
                
        else:
            subject = f"{model} is ended but something wrong."
            message = f"something wrong to show the result. see details in server {server_number}."
    except:
        subject = f"{model} is ended but something wrong."
        message = f"something wrong to show the result. see details in server {server_number}."
        
    recipient_emails = ["junghokim@spa.hanyang.ac.kr", "hjshin@spa.hanyang.ac.kr"]
    for recipient_email in recipient_emails:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(message, 'plain'))
        text = msg.as_string()
        server.sendmail(sender_email, recipient_email, text)
        
    server.quit()
    print("sent the result")

if __name__=="__main__":
    result_txt = sys.argv[1]
    model = result_txt.split('/')[-1]
    
    try:
        server_number = sys.argv[2]
    except:
        server_number = ""
    for log_path in sorted(os.listdir(result_txt), reverse=True):
        if '.log' in log_path and '.json' not in log_path:
            result_txt = osp.join(result_txt, log_path)
            break
    try:
        result_line_list = []
        f = open(result_txt, 'r')
        while True:
            line = f.readline()
            if not line: break
            result_line_list.append(line)
    except:
        result_line_list = None
        
    send_mail(result_line_list, model, server_number)