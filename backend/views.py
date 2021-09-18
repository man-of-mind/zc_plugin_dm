import json, uuid, re
from django.http import response
from django.http.response import JsonResponse
from django.shortcuts import render
from rest_framework.response import Response
from rest_framework.decorators import api_view
from rest_framework import status
import requests
from .db import *

# Import Read Write function to Zuri Core
from .resmodels import *
from .serializers import *
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.pagination import PageNumberPagination

from .centrifugo_handler import centrifugo_client

def index(request):
    context = {}
    return render(request, "index.html", context)


# Shows basic information about the DM plugin
def info(request):
    info = {
        "message": "Plugin Information Retrieved",
        "data": {
            "type": "Plugin Information",
            "plugin_info": {
                "name": "DM Plugin",
                "description": [
                    "Zuri.chat plugin",
                    "DM plugin for Zuri Chat that enables users to send messages to each other",
                ],
            },
            "scaffold_structure": "Monolith",
            "team": "HNG 8.0/Team Orpheus",
            "sidebar_url": "https://dm.zuri.chat/api/v1/sidebar",
            "homepage_url": "https://dm.zuri.chat/",
        },
        "success": "true",
    }

    return JsonResponse(info, safe=False)


def verify_user(token):
    """
    Call Endpoint for verification of user (sender)
    It takes in either token or cookies and returns a python dictionary of 
    user info if 200 successful or 401 unathorized if not
    """
    url = "https://api.zuri.chat/auth/verify-token"
    
    headers={}
    if '.' in token:
        headers['Authorization'] = f'Bearer {token}'
    else:
        headers['Cookie'] = token
        
    response = requests.get(url, headers=headers)
    response = response.json()

    return response

	
# Returns the json data of the sidebar that will be consumed by the api
# The sidebar info will be unique for each logged in user
# user_id will be gotten from the logged in user
# All data in the message_rooms will be automatically generated from zuri core


def side_bar(request):
    collections = "dm_rooms"
    org_id = request.GET.get("org", None)
    user = request.GET.get("user", None)
    rooms = get_user_rooms(collections, org_id, user)

    side_bar = {
        "name": "DM Plugin",
        "description": "Sends messages between users",
        "plugin_id": "6135f65de2358b02686503a7",
        "organisation_id": f"{org_id}",
        "user_id": f"{user}",
        "group_name": "DM",
        "show_group": False,
        "public_rooms": [],
        "joined_rooms": rooms,
        # List of rooms/collections created whenever a user starts a DM chat with another user
        # This is what will be displayed by Zuri Main
    }
    return JsonResponse(side_bar, safe=False)


@swagger_auto_schema(
    methods=["post"],
    request_body=MessageSerializer,
    responses={201: MessageResponse, 400: "Error: Bad Request"},
)
@api_view(["POST"])
def send_message(request, room_id ):
    """
    This endpoint is used to send message to user in rooms.
    It checks if room already exist before sending data.
    It makes a publish event to centrifugo after data
    is persisted
    """
    request.data['room_id'] = room_id
    print(request)
    serializer = MessageSerializer(data=request.data)

    if serializer.is_valid():
        data = serializer.data
        room_id = data["room_id"]  # room id gotten from client request

        room = DB.read("dm_rooms", {"_id": room_id})
        if room:
            if data["sender_id"] in room.get("room_user_ids", []):

                response = DB.write("dm_messages", data=serializer.data)
                if response.get("status", None) == 200:

                    response_output = {
                        "status": response["message"],
                        "event": "message_create",
                        "message_id": response["data"]["object_id"],
                        "room_id": room_id,
                        "thread": False,
                        "data": {
                            "sender_id": data["sender_id"],
                            "message": data["message"],
                            "created_at": data["created_at"],
                        },
                    }

                    centrifugo_data = send_centrifugo_data(room=room_id, data=response_output)  # publish data to centrifugo
                    # print(centrifugo_data)
                    if centrifugo_data["message"].get("error", None) == None:
                        return Response(data=response_output, status=status.HTTP_201_CREATED)
                return Response(data="data not sent", status=status.HTTP_424_FAILED_DEPENDENCY)
            return Response("sender not in room", status=status.HTTP_400_BAD_REQUEST)
        return Response("room not found", status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    methods=["post"],
    request_body=ThreadSerializer,
    responses={201: ThreadResponse, 400: "Error Response"},
)
@api_view(["POST"])
def send_thread_message(request,room_id, message_id):
    """
    This endpoint is used send messages as a thread
    under a message. It takes a message ID and
    validates if the message exists, then sends
    a publish event to centrifugo after
    thread message is persisted.
    """
    request.data['message_id'] = message_id
    serializer = ThreadSerializer(data=request.data)

    if serializer.is_valid():
        data = serializer.data
        message_id = data["message_id"]
        sender_id = data["sender_id"]

        message = DB.read("dm_messages", {"_id": message_id, "room_id":room_id})  # fetch message from zc 

        if message:
            threads = message.get("threads", [])  # get threads
            del data["message_id"]  # remove message id from request to zc core
            data["_id"] = str(uuid.uuid1())  # assigns an id to each message in thread
            threads.append(data)  # append new message to list of thread
            
            room = DB.read("dm_rooms",{"_id":message["room_id"]})
            if sender_id in room.get("room_user_ids", []):

                response = DB.update("dm_messages", message["_id"], {"threads": threads} )  # update threads in db
                if response.get("status", None) == 200:

                    response_output = {
                        "status": response["message"],
                        "event": "thread_message_create",
                        "thread_id": data["_id"],
                        "room_id": message["room_id"],
                        "message_id": message["_id"],
                        "thread": True,
                        "data": {
                            "sender_id": data["sender_id"],
                            "message": data["message"],
                            "created_at": data["created_at"],
                        },
                    }

                    centrifugo_data = send_centrifugo_data(room=message["room_id"], data=response_output)  # publish data to centrifugo
                    if centrifugo_data["message"].get("error", None) == None:
                        return Response(data=response_output, status=status.HTTP_201_CREATED)
                return Response("data not sent", status=status.HTTP_424_FAILED_DEPENDENCY)
            return Response("sender not in room", status=status.HTTP_404_NOT_FOUND)
        return Response("message or room not found", status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    methods=["post"],
    request_body=RoomSerializer,
    responses={201: CreateRoomResponse, 400: "Error: Bad Request"},
)
@api_view(["POST"])
def create_room(requests):
    """
    This function is used to create a room between 2 users.
    It takes the id of the users involved, sends a write request to the database .
    Then returns the room id when a room is successfully created
    """
    serializer = RoomSerializer(data=requests.data)

    if serializer.is_valid():
        response = DB.write("dm_rooms", data=serializer.data)
        data = response.get("data").get("object_id")
        if response.get("status") == 200:
            response_output = {"room_id": data}
            return Response(data=response_output, status=status.HTTP_201_CREATED)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    methods=["get"],
    query_serializer=UserRoomsSerializer,
    responses={400: "Error: Bad Request"},
)
@api_view(["GET"])
def getUserRooms(request):
    """
    This is used to retrieve all rooms a user is currently active in.
    It takes in a user_id as query param and returns the rooms for that user or a 204 status code
    if there is no room for the user_id or an invalid user_id.
    If the user_id is not provided, a 400 status code is returned.
    """
    if request.method == "GET":
        res = get_rooms(request.GET.get("user_id", None))
        query_param_serializer = UserRoomsSerializer(data=request.GET.dict())
        if query_param_serializer.is_valid():
            if res == None:
                return Response(data="No rooms available", status=status.HTTP_204_NO_CONTENT)
            return Response(res, status=status.HTTP_200_OK)
        return Response(query_param_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    methods=["get"],
    query_serializer=GetMessageSerializer,
    responses={201: MessageResponse, 400: "Error: Bad Request"},
)
@api_view(["GET"])
def room_messages(request, room_id):
    """
    This is used to retrieve messages in a room.
    It returns a 204 status code if there is no message in the room. 
    The messages can be filter by adding date in the query, 
    it also returns a 204 status if there is no messages.
    """
    if request.method == "GET":
        paginator = PageNumberPagination()
        paginator.page_size = 20
        date = request.GET.get("date", None)
        params_serializer = GetMessageSerializer(data=request.GET.dict()) 
        if params_serializer.is_valid():
            room = DB.read("dm_rooms", {"_id": room_id})
            if room:
                messages = get_room_messages(room_id)
                if date != None:
                    messages_by_date = get_messages(messages, date)
                    if messages_by_date == None or "message" in messages_by_date:
                        return Response(data="No messages available", status=status.HTTP_204_NO_CONTENT)
                    messages_page = paginator.paginate_queryset(messages_by_date, request)
                    return paginator.get_paginated_response(messages_page)
                else:
                    if messages == None or "message" in messages:
                        return Response(data="No messages available", status=status.HTTP_204_NO_CONTENT)
                    result_page = paginator.paginate_queryset(messages, request)
                    return paginator.get_paginated_response(result_page)
            return Response(data="No such room", status=status.HTTP_400_BAD_REQUEST)
        return Response(params_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    methods=["get"],
    query_serializer=RoomInfoSerializer,
    responses={201: RoomInfoResponse, 400: "Error: Bad Request"},
)
@api_view(["GET"])
def room_info(request):
    """
    This is used to retrieve information about a room.
    """
    room_id = request.GET.get("room_id", None)
    # org_id = request.GET.get("org_id", None)
    room_collection = "dm_rooms"
    rooms = DB.read(room_collection)
    print(rooms)
    if rooms is not None:
        for current_room in rooms:
            if current_room["_id"] == room_id:
                if "room_user_ids" in current_room:
                    room_user_ids = current_room["room_user_ids"]
                else:
                    room_user_ids = ""
                if "created_at" in current_room:
                    created_at = current_room["created_at"]
                else:
                    created_at = ""
                if "org_id" in current_room:
                    org_id = current_room["org_id"]
                else:
                    org_id ="6133c5a68006324323416896"
                if len(room_user_ids)>3:
                    text = f" and {len(room_user_ids)-2} others"
                elif len(room_user_ids) == 3:
                    text = "and 1 other"
                else:
                    text = " only"
                room_data = {
                    "room_id": room_id,
                    "org_id": org_id,
                    "room_user_ids": room_user_ids,
                    "created_at": created_at,
                    "description": f"This room contains the coversation between {room_user_ids[0]} and {room_user_ids[1]}{text}",
                    "Number of users": f"{len(room_user_ids)}"
                }
                return Response(data=room_data, status=status.HTTP_200_OK)
        return Response(data="No such Room", status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_400_BAD_REQUEST)


# /code for updating room


@api_view(["GET", "POST"])
def edit_room(request, pk):
    try:
        message= DB.read("dm_messages",{"id":pk})
    except:
        return JsonResponse({'message': 'The room does not exist'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        singleRoom = DB.read("dm_messages",{"id": pk})
        return JsonResponse(singleRoom)
    else:
        room_serializer = MessageSerializer(message, data=request.data,partial = True)
        if room_serializer.is_valid():
            room_serializer.save()
            data = room_serializer.data
            # print(data)
            response = DB.update("dm_messages", pk, data)
            return Response(room_serializer.data)
        return Response(room_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    return Response(data="No Rooms", status=status.HTTP_400_BAD_REQUEST)




@swagger_auto_schema(methods=['get'], responses={201: MessageLinkResponse, 400: "Error: Bad Request"})
@api_view(['GET'])
def copy_message_link(request, message_id):
    """
    This is used to retrieve a single message. It takes a message_id as query params.
    If message_id is provided, it returns a dictionary with information about the message,
    or a 204 status code if there is no message with the same message id.
    I will use the message information returned to generate a link which contains a room_id and a message_id
    """
    if request.method == "GET":
        message = DB.read("dm_messages", {"id": message_id})
        room_id = message["room_id"]
        message_info = {
            "room_id": room_id,
            "message_id": message_id,
            "link": f"https://dm.zuri.chat/getmessage/{room_id}/{message_id}",
        }
        return Response(data=message_info, status=status.HTTP_200_OK)
    else:
        return JsonResponse(
            {"message": "The message does not exist"}, status=status.HTTP_404_NOT_FOUND
        )


def read_message_link(request, room_id, message_id):
    """
    This is used to retrieve a single message. It takes a message_id as query params.
    or a 204 status code if there is no message with the same message id.
    I will use the message information returned to generate a link which contains a room_id and a message_id
    """

    if request.method == "GET":
        message = DB.read("dm_messages", {"id": message_id, "room_id": room_id})
        return Response(data=message, status=status.HTTP_200_OK)
    else:
        return JsonResponse({'message': 'The message does not exist'}, status=status.HTTP_404_NOT_FOUND)




@api_view(["GET"])
def get_links(request, room_id):
    """
    Search messages in a room and return all links found
    """
    url_pattern =  r"^(?:ht|f)tp[s]?://(?:www.)?.*$"
    regex = re.compile(url_pattern)
    matches = []
    messages = DB.read(
        "dm_messages", filter={"room_id": room_id})
    if messages is not None:
        for message in messages:
            for word in message.get("message").split(" "):
                match = regex.match(word)
                if match:
                    matches.append(
                        {"link": str(word), "timestamp": message.get("created_at")})
        data = {
            "links": matches,
            "room_id": room_id
        }
        return Response(data=data, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_404_NOT_FOUND)




@api_view(["POST"])
def save_bookmark(request, room_id):
    """
    save a link as bookmark in a room
    """
    try:
        serializer = BookmarkSerializer(data=request.data)
        room = DB.read("dm_rooms", {"id": room_id})
        bookmarks = room["bookmarks"] or []
    except Exception as e:
        print(e)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if serializer.is_valid() and bookmarks is not None:
        bookmarks.append(serializer.data)
        data = {"bookmarks": bookmarks}
        response = DB.update("dm_rooms", room_id, data=data)
        if response.get("status") == 200:
            return Response(data=serializer.data, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(methods=['post'], request_body=CookieSerializer, responses={400: "Error: Bad Request"})
@api_view(['GET', 'POST'])
def organization_members(request):
    """
    This endpoint returns a list of members for an organization.
    :returns: json response -> a list of objects (members) or 401_Unauthorized messages.
    
    GET: simulates production - if request is get, either token or cookie gotten from FE will be used,
    and authorization should take places automatically.
    
    POST: simulates testing - if request is post, send the cookies through the post request, it would be added
    manually to grant access, PS: please note cookies expire after a set time of inactivity.
    """
    url = f"https://api.zuri.chat/organizations/{ORG_ID}/members"
    
    if request.method == "GET":
        headers={}
        
        if 'Authorization' in request.headers:
            headers['Authorization'] = request.headers['Authorization']
        else:
            headers['Cookie'] = request.headers['Cookie']
        
        response = requests.get(url, headers=headers)
    
    elif request.method == "POST":
        cookie_serializer = CookieSerializer(data=request.data)
    
        if cookie_serializer.is_valid():
            cookie = cookie_serializer.data['cookie']
            response = requests.get(url, headers={'Cookie': cookie})
        else:
            return Response(cookie_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
		
    if response.status_code == 200:
        response = response.json()['data']
        return Response(response, status = status.HTTP_200_OK)
    return Response(response.json(), status = status.HTTP_401_UNAUTHORIZED)




@api_view(["GET"])
def retrieve_bookmarks(request, room_id):
    """
    Retrieves all saved bookmarks in the room
    """
    try:
        room = DB.read("dm_rooms", {"id": room_id})
        bookmarks = room["bookmarks"] or []
    except Exception as e:
        print(e)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if bookmarks is not None:
        serializer = BookmarkSerializer(data=bookmarks, many=True)
        if serializer.is_valid():
            return Response(data=serializer.data, status=status.HTTP_200_OK)
        return Response(status=status.HTTP_400_BAD_REQUEST)
    return Response(status=status.HTTP_404_NOT_FOUND)


@api_view(["PUT"])
def mark_read(request, message_id):
    """
    mark a message as read and unread
    """
    try:
        message = DB.read("dm_messages", {"id": message_id})
        read = message["read"]
    except Exception as e:
        print(e)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE) 
    data = {"read": not read}
    response = DB.update("dm_messages", message_id, data=data)
    message = DB.read("dm_messages", {"id": message_id})
    
    if response.get("status") == 200:
       return Response(data=data, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_400_BAD_REQUEST)

@api_view(["PUT"])
def pinned_message(request, message_id):
    """
    This is used to pin a message.
    The message_id is passed to it which
    reads through the database, gets the room id,
    generates a link and then add it to the pinned key value.

    If the link already exist, it would greet you with a nice response from the developer that wrote it.
    """
    try:
        message = DB.read("dm_messages", {"id": message_id})
        room_id = message["room_id"]
        room = DB.read("dm_rooms", {"id": room_id})
        pin = room["pinned"] or []
        link = f"https://dm.zuri.chat/"+ f"{room_id}"+"/"+f"{message_id}"+"/pinnedmessage"
    except Exception as e:
        print(e)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if link not in pin:
        pin.append(link)
        data = {"pinned": pin}
        response = DB.update("dm_rooms", room_id, data)
        room = DB.read("dm_rooms", {"id": room_id})
        if response.get("status") == 200:
            return Response(data=data, status=status.HTTP_200_OK)
    return Response(data = "Already exist! why do you want to break my code?", status=status.HTTP_409_CONFLICT)


@api_view(["DELETE"])
def delete_pinned_message(request, message_id):
    """
    This is used to delete a pinned message.
    It takes in the message id, gets the room id, generates a link and then check
    if that link exists. If it exists, it deletes it
    if not,...
    """
    try:
        message = DB.read("dm_messages", {"id": message_id})
        room_id = message["room_id"]
        room = DB.read("dm_rooms", {"id": room_id})
        pin = room["pinned"] or []
        link = f"https://dm.zuri.chat/"+ f"{room_id}"+"/"+f"{message_id}"+"/pinnedmessage"
    except Exception as e:
        print(e)
        return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if link in pin:
        print("YES")
        pin.remove(link)
        data = {"pinned": pin}
        response = DB.update("dm_rooms", room_id, data)
        room = DB.read("dm_rooms", {"id": room_id})
        if response.get("status") == 200:
            return Response(data=data, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
def message_filter(request, room_id):
    """
    Fetches all the messages in a room, and sort it out according to time_stamp.
    """
    if request.method == "GET":
        room = DB.read("dm_rooms", {"id": room_id})
        # room = "613b2db387708d9551acee3b"

        if room is not None :
            all_messages = DB.read("dm_messages", filter={"room_id":room_id})
            if all_messages is not None:
                message_timestamp_filter = sorted(all_messages, key=lambda k: k['created_at'])
                return Response(message_timestamp_filter, status=status.HTTP_200_OK)
            return Response(data="No messages available", status=status.HTTP_204_NO_CONTENT)
        return Response(data="No Room or Invalid Room", status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(methods=['delete'], request_body=DeleteMessageSerializer, responses={400: "Error: Bad Request"})
@api_view(["DELETE"])
def delete_message(request):
    """
    Deletes a message after taking the message id
    """

    if request.method == "DELETE":
        message_id=request.GET.get('message_id')
        message = DB.read('dm_messages', {'_id':message_id})
        if message:
            response=DB.delete('dm_messages', message_id)
            return Response(response, status.HTTP_200_OK)
        else:
            return Response("No such message", status.HTTP_404_NOT_FOUND)
    return Response(status.HTTP_405_METHOD_NOT_ALLOWED)


@swagger_auto_schema(methods=['get'], responses={201: UserProfileResponse, 400: "Error: Bad Request"})
@api_view(["GET"])
def user_profile(request, org_id, user_id):
    """
    Retrieves the user details of a member in an organization using a unique user_id
    If request is successful, a json output of select user details is returned
    Elif login session is expired or wrong details were entered, a 401 response is returned
    Else a 405 response returns if a wrong method was used
    """
    url = f"https://api.zuri.chat/organizations/{org_id}/members/{user_id}"
    #url = f"https://dm.zuri.chat/api/v1/get_organization_members/{user_id}"

    if request.method == "GET":
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()["data"]
            output = {
                "name": data["name"],
                "display_name": data["display_name"],
                "bio": data["bio"],
                "pronouns": data["pronouns"],
                "email": data["email"],
                "phone": data["phone"],
                "status": data["status"]
            }
            return Response(output, status = status.HTTP_200_OK)
        return Response(response.json(), status = status.HTTP_401_UNAUTHORIZED)
    return Response(status.HTTP_405_METHOD_NOT_ALLOWED)
